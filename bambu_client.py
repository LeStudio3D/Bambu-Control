"""
bambu_client.py

Gère un pool d'imprimantes Bambu Lab, chacune avec son propre thread de
fond (connexion MQTT + caméra + reconnexion automatique + historique
SQLite). Le PrinterManager expose les opérations dont Flask a besoin,
indexées par printer_id.
"""
import io
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import bambulabs_api as bl

import config
import models

DB_PATH = config.DB_PATH
CAMERA_REFRESH_SECONDS = config.CAMERA_POLL_INTERVAL
HISTORY_SAVE_SECONDS = config.HISTORY_SAVE_INTERVAL

UPLOAD_DIR = Path(config.UPLOAD_TMP_DIR)
UPLOAD_DIR.mkdir(exist_ok=True)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            gcode_state TEXT,
            file_name TEXT,
            mc_percent INTEGER,
            remaining_time_min INTEGER,
            current_layer INTEGER,
            total_layers INTEGER,
            nozzle_temp REAL,
            bed_temp REAL,
            chamber_temp REAL,
            print_speed INTEGER,
            wifi_signal TEXT,
            light_state TEXT,
            raw_json TEXT
        )
    """)
    conn.commit()
    return conn


def extract_ams_from_raw(raw: dict) -> list:
    """
    Parse la section AMS directement depuis le JSON brut du MQTT, en
    contournant le parsing fourni par bambulabs_api (qui ignore certains
    bacs à cause d'un test `if tray_n:` qui échoue quand n=0).
    """
    ams_info = raw.get("print", {}).get("ams", {})
    if not ams_info or ams_info.get("ams_exist_bits", "0") == "0":
        return []

    slots = []
    for ams_unit in ams_info.get("ams", []):
        ams_id = ams_unit.get("id", "0")
        for tray in ams_unit.get("tray", []):
            tray_type = tray.get("tray_type", "") or ""
            remain_raw = tray.get("remain", -1)
            try:
                remain = int(remain_raw)
            except (TypeError, ValueError):
                remain = -1
            slots.append({
                "ams_id": ams_id,
                "tray_id": tray.get("id"),
                "empty": tray_type == "",
                "type": tray_type or None,
                "color": tray.get("tray_color") or None,
                "remaining": remain if remain >= 0 else None,
            })
    return slots


class PrinterWorker:
    """Gère une imprimante : connexion, threads de fond, état partagé."""

    def __init__(self, printer_cfg: dict, db_conn):
        self.id = printer_cfg["id"]
        self.name = printer_cfg["name"]
        self.model = printer_cfg.get("model", "OTHER")
        self.ip = printer_cfg["ip"]
        self.access_code = printer_cfg["access_code"]
        self.serial = printer_cfg["serial"]

        override = printer_cfg.get("has_chamber_override")
        self.has_chamber_sensor = (
            override if override is not None
            else models.default_has_chamber(self.model)
        )

        self.db_conn = db_conn
        self.lock = threading.Lock()
        self.printer = None
        self.connected = False
        self.last_error = None
        self.status = {}
        self.camera_jpeg = None
        self.camera_timestamp = None
        self._stop = False

    def snapshot(self):
        with self.lock:
            data = dict(self.status)
        data["has_chamber_sensor"] = self.has_chamber_sensor
        data["printer_name"] = self.name
        data["model"] = self.model
        return data

    def get_camera(self):
        with self.lock:
            return self.camera_jpeg, self.camera_timestamp

    def _read_status(self, printer, raw: dict) -> dict:
        mqtt = printer.mqtt_client
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "gcode_state": _safe(lambda: str(printer.get_state())),
            "file_name": _safe(lambda: printer.get_file_name()),
            "mc_percent": _safe(lambda: printer.get_percentage()),
            "remaining_time_min": _safe(lambda: printer.get_time()),
            "current_layer": _safe(lambda: printer.current_layer_num()),
            "total_layers": _safe(lambda: printer.total_layer_num()),
            "nozzle_temp": _safe(lambda: printer.get_nozzle_temperature()),
            "nozzle_target": _safe(lambda: mqtt.get_nozzle_temperature_target()),
            "bed_temp": _safe(lambda: printer.get_bed_temperature()),
            "bed_target": _safe(lambda: mqtt.get_bed_temperature_target()),
            "chamber_temp": _safe(lambda: printer.get_chamber_temperature())
                if self.has_chamber_sensor else None,
            "print_speed": _safe(lambda: printer.get_print_speed()),
            "wifi_signal": _safe(lambda: printer.wifi_signal()),
            "light_state": _safe(lambda: printer.get_light_state()),
            "fan_part": _safe(lambda: mqtt.get_part_fan_speed()),
            "fan_aux": _safe(lambda: mqtt.get_aux_fan_speed()),
            "fan_chamber": _safe(lambda: mqtt.get_chamber_fan_speed()),
            "ams": extract_ams_from_raw(raw),
        }

    def _store_snapshot(self, data: dict, raw: dict):
        try:
            self.db_conn.execute("""
                INSERT INTO snapshots (
                    printer_id, timestamp, gcode_state, file_name, mc_percent,
                    remaining_time_min, current_layer, total_layers,
                    nozzle_temp, bed_temp, chamber_temp, print_speed,
                    wifi_signal, light_state, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.id, data["timestamp"], data["gcode_state"], data["file_name"],
                data["mc_percent"], data["remaining_time_min"], data["current_layer"],
                data["total_layers"], data["nozzle_temp"], data["bed_temp"],
                data["chamber_temp"], data["print_speed"], str(data["wifi_signal"]),
                data["light_state"], json.dumps(raw, ensure_ascii=False, default=str),
            ))
            self.db_conn.commit()
        except Exception as e:
            print(f"[{self.id}] Erreur SQLite : {e}")

    def get_history(self, limit=200):
        cur = self.db_conn.execute(
            "SELECT * FROM snapshots WHERE printer_id = ? ORDER BY id DESC LIMIT ?",
            (self.id, limit),
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return list(reversed(rows))

    def run(self):
        printer = bl.Printer(self.ip, self.access_code, self.serial)

        while not self._stop:
            try:
                print(f"[{self.id}] Connexion a {self.ip}...")
                printer.connect()
                time.sleep(2)
                try:
                    printer.camera_start()
                    time.sleep(2)
                except Exception as e:
                    with self.lock:
                        self.last_error = f"Camera : {e}"

                with self.lock:
                    self.printer = printer
                    self.connected = True
                    self.last_error = None

                print(f"[{self.id}] Connecte.")
                last_db_write = 0

                while not self._stop:
                    raw = _safe(lambda: printer.mqtt_dump(), {})
                    data = self._read_status(printer, raw)

                    try:
                        image = printer.get_camera_image()
                        buf = io.BytesIO()
                        image.save(buf, "JPEG", quality=80)
                        jpeg_bytes = buf.getvalue()
                        with self.lock:
                            self.camera_jpeg = jpeg_bytes
                            self.camera_timestamp = datetime.now().isoformat(timespec="seconds")
                    except Exception:
                        pass  # camera peut etre temporairement indisponible, pas fatal

                    with self.lock:
                        self.status = data

                    now_ts = time.time()
                    if now_ts - last_db_write >= HISTORY_SAVE_SECONDS:
                        self._store_snapshot(data, raw)
                        last_db_write = now_ts

                    time.sleep(CAMERA_REFRESH_SECONDS)

            except Exception as e:
                with self.lock:
                    self.connected = False
                    self.last_error = str(e)
                print(f"[{self.id}] Connexion perdue / erreur : {e}. "
                      f"Nouvelle tentative dans 10s...")
                try:
                    printer.disconnect()
                except Exception:
                    pass
                time.sleep(10)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self._stop = True

    def upload_and_maybe_print(self, filepath: Path, start_print: bool,
                               plate_number="1", use_ams=True):
        with self.lock:
            printer = self.printer
            connected = self.connected
        if not printer or not connected:
            raise RuntimeError("Imprimante non connectee")

        with open(filepath, "rb") as f:
            remote_path = printer.upload_file(f, filename=filepath.name)

        started = None
        if start_print:
            started = printer.start_print(filepath.name, plate_number, use_ams=use_ams)

        return remote_path, started

    # ── Contrôle ──────────────────────────────────────────
    def _require_printer(self):
        with self.lock:
            printer = self.printer
            connected = self.connected
        if not printer or not connected:
            raise RuntimeError("Imprimante non connectée")
        return printer

    def set_light(self, on: bool) -> bool:
        printer = self._require_printer()
        return printer.turn_light_on() if on else printer.turn_light_off()

    def set_nozzle_temperature(self, value: int) -> bool:
        printer = self._require_printer()
        return printer.set_nozzle_temperature(value)

    def set_bed_temperature(self, value: int) -> bool:
        printer = self._require_printer()
        return printer.set_bed_temperature(value)

    def set_speed_level(self, level: int) -> bool:
        printer = self._require_printer()
        return printer.mqtt_client.set_print_speed_lvl(level)

    def set_fan(self, fan: str, percent: float) -> bool:
        printer = self._require_printer()
        speed = max(0.0, min(1.0, percent / 100.0))
        if fan == "part":
            return printer.set_part_fan_speed(speed)
        elif fan == "aux":
            return printer.set_aux_fan_speed(speed)
        elif fan == "chamber":
            return printer.set_chamber_fan_speed(speed)
        raise ValueError(f"Ventilateur inconnu : {fan}")


class PrinterManager:
    """Registre de tous les PrinterWorker actifs, indexes par printer_id."""

    def __init__(self):
        self.db_conn = init_db()
        self.workers = {}
        self._lock = threading.Lock()

    def load_from_store(self, printer_configs: list):
        with self._lock:
            for cfg in printer_configs:
                if cfg["id"] not in self.workers:
                    worker = PrinterWorker(cfg, self.db_conn)
                    self.workers[cfg["id"]] = worker
                    worker.start()

    def add_and_start(self, printer_cfg: dict):
        with self._lock:
            worker = PrinterWorker(printer_cfg, self.db_conn)
            self.workers[printer_cfg["id"]] = worker
            worker.start()

    def remove(self, printer_id: str):
        with self._lock:
            worker = self.workers.pop(printer_id, None)
            if worker:
                worker.stop()

    def get(self, printer_id: str):
        return self.workers.get(printer_id)

    def list_ids(self):
        return list(self.workers.keys())


manager = PrinterManager()
