"""
bambu_client.py

Gère un pool d'imprimantes Bambu Lab. Chaque imprimante a un PrinterWorker
avec son thread de fond : connexion MQTT, caméra, détection hors ligne,
reconnexion automatique et enregistrement de l'historique SQLite.
Le PrinterManager expose ce dont Flask a besoin, indexé par printer_id.
"""
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import bambulabs_api as bl

import camera
import commands
import config
import history as history_module
import models
import sd_card
from history import HistoryDB  # noqa: F401  (gardé pour compatibilité : bc.HistoryDB)

log = logging.getLogger("bambu")

UPLOAD_DIR = Path(config.UPLOAD_TMP_DIR)

# États de connexion exposés à l'interface
CONNECTING = "connecting"
ONLINE = "online"
OFFLINE = "offline"



def _safe(fn, default=None):
    """Lit une valeur de la lib sans faire planter la boucle si elle manque."""
    try:
        return fn()
    except Exception:
        return default


# ── AMS ───────────────────────────────────────────────────
# Numérotation Bambu : AMS / AMS 2 Pro / AMS lite = 0, 1, 2, 3 (4 emplacements,
# A1…A4, B1…B4…) ; AMS HT = 128, 129… (1 seul emplacement chacun).
AMS_HT_BASE = 128
# Température maximale de séchage par modèle (les autres ne sèchent pas)
DRYING_MAX_TEMP = {"AMS 2 Pro": 65, "AMS HT": 85}
# Préfixe du nom de module (réponse « get_version ») → modèle
AMS_MODULE_PREFIXES = {"n3f": "AMS 2 Pro", "n3s": "AMS HT", "ams_f1": "AMS lite", "ams": "AMS"}
OPEN_FRAME_MODELS = ("A1", "A1_MINI", "A2L")   # ces imprimantes n'acceptent que l'AMS lite


def _int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def ams_unit_name(ams_id: int) -> str:
    return f"HT{ams_id - AMS_HT_BASE + 1}" if ams_id >= AMS_HT_BASE else chr(ord("A") + ams_id)


def ams_models_from_modules(modules: list) -> dict:
    """{ams_id: modèle} à partir de la liste des modules (« n3f/0 » → AMS 2 Pro n° 0)."""
    found = {}
    for module in modules or []:
        name = str(module.get("name") or "")
        prefix, _, number = name.partition("/")
        if prefix in AMS_MODULE_PREFIXES and number.isdigit():
            found[int(number)] = AMS_MODULE_PREFIXES[prefix]
    return found


def guess_ams_model(ams_id: int, unit: dict, known: dict, printer_model: str) -> str:
    if ams_id in known:
        return known[ams_id]
    if ams_id >= AMS_HT_BASE:
        return "AMS HT"
    if printer_model in OPEN_FRAME_MODELS:
        return "AMS lite"
    if "dry_time" in unit or "dry_setting" in unit:   # seuls les AMS chauffants envoient ces champs
        return "AMS 2 Pro"
    return "AMS"


# Raisons « dry_sf_reason » connues (d'après Bambu Studio / Bambuddy)
DRY_BLOCK_REASONS = {
    1: "branche l'adaptateur secteur de l'AMS",
    8: "branche l'adaptateur secteur de l'AMS",
}


def remote_dry_status(unit: dict) -> tuple[bool, list[str]]:
    """
    Le firmware accepte-t-il le séchage à distance ? Seules les imprimantes
    compatibles envoient « dry_sf_reason » (comme Bambu Studio, on s'y fie).
    Renvoie (compatible, [raisons qui bloquent le séchage maintenant]).
    """
    zones = unit.get("zones") if isinstance(unit.get("zones"), list) else []
    sources = [unit] + [z for z in zones if isinstance(z, dict)]
    present = [s.get("dry_sf_reason") for s in sources if "dry_sf_reason" in s]
    if not present:
        return False, []
    reasons = []
    for value in present:
        for code in value if isinstance(value, list) else [value]:
            code = _int(code, 0)
            if code:
                text = DRY_BLOCK_REASONS.get(code, f"l'imprimante refuse le séchage (raison n° {code})")
                if text not in reasons:
                    reasons.append(text)
    return True, reasons


def merge_ams(state: dict, update: dict) -> dict:
    """
    Fusionne une mise à jour AMS (souvent partielle : juste « tray_now », ou un
    seul emplacement) dans l'état connu. bambulabs_api remplace tout le bloc AMS
    à chaque message partiel : la liste des AMS disparaissait par moments.
    """
    merged = dict(state)
    for key, value in update.items():
        if key != "ams" or not isinstance(value, list):
            merged[key] = value
            continue
        units = {str(u.get("id")): dict(u) for u in merged.get("ams") or [] if isinstance(u, dict)}
        for unit in value:
            if not isinstance(unit, dict):
                continue
            uid = str(unit.get("id"))
            current = units.get(uid, {})
            trays = {str(t.get("id")): dict(t) for t in current.get("tray") or [] if isinstance(t, dict)}
            for tray in unit.get("tray") or []:
                if isinstance(tray, dict):
                    trays[str(tray.get("id"))] = {**trays.get(str(tray.get("id")), {}), **tray}
            units[uid] = {**current, **{k: v for k, v in unit.items() if k != "tray"},
                          "tray": sorted(trays.values(), key=lambda t: _int(t.get("id"), 0))}
        merged["ams"] = sorted(units.values(), key=lambda u: _int(u.get("id"), 0))
    if merged.get("ams_exist_bits") == "0":     # AMS débranché
        merged["ams"] = []
    return merged


def _ams_section(raw: dict) -> dict:
    ams_info = raw.get("print", {}).get("ams", {})
    if not ams_info or ams_info.get("ams_exist_bits", "0") == "0":
        return {}
    return ams_info


def extract_ams_from_raw(raw: dict) -> list:
    """
    Tous les emplacements de tous les AMS (liste à plat), lus directement dans
    le JSON brut du MQTT (bambulabs_api ignore le bac n°0).
    global_index = valeur à mettre dans ams_mapping pour un lancement.
    """
    ams_info = _ams_section(raw)
    active_tray = _int(ams_info.get("tray_now"), 255)   # 255 = aucun, 254 = bobine externe
    slots = []
    for ams_unit in ams_info.get("ams", []) if ams_info else []:
        ams_id = _int(ams_unit.get("id"), -1)
        for tray in ams_unit.get("tray", []):
            tray_id = _int(tray.get("id"), -1)
            tray_type = tray.get("tray_type", "") or ""
            remain = _int(tray.get("remain"), -1)
            if ams_id < 0 or tray_id < 0:
                global_index, label, active = -1, "?", False
            elif ams_id >= AMS_HT_BASE:            # AMS HT : un seul emplacement
                global_index, label = ams_id, ams_unit_name(ams_id)
                active = active_tray in (ams_id, ams_id * 4 + tray_id)
            else:
                global_index = ams_id * 4 + tray_id
                label = f"{ams_unit_name(ams_id)}{tray_id + 1}"   # A1…A4, B1…
                active = global_index == active_tray
            slots.append({
                "ams_id": str(ams_id),
                "tray_id": tray.get("id"),
                "global_index": global_index,
                "label": label,
                "active": active,
                "empty": tray_type == "",
                "type": tray_type or None,
                "color": tray.get("tray_color") or None,
                "remaining": remain if remain >= 0 else None,
            })
    return slots


def extract_ams_units(raw: dict, known_models: dict, printer_model: str) -> list:
    """Un résumé par AMS : modèle, humidité, température, séchage en cours, emplacements."""
    ams_info = _ams_section(raw)
    slots = extract_ams_from_raw(raw)
    units = []
    for unit in ams_info.get("ams", []) if ams_info else []:
        ams_id = _int(unit.get("id"), -1)
        if ams_id < 0:
            continue
        model = guess_ams_model(ams_id, unit, known_models, printer_model)
        can_dry = model in DRYING_MAX_TEMP
        remote_dry, blocked = remote_dry_status(unit)
        dry_setting = unit.get("dry_setting") if isinstance(unit.get("dry_setting"), dict) else {}
        dry_left = _int(unit.get("dry_time"), 0) or 0
        humidity_pct = _int(unit.get("humidity_raw"))
        units.append({
            "id": ams_id,
            "name": f"AMS {ams_unit_name(ams_id)}",
            "model": model,
            "humidity_pct": humidity_pct if humidity_pct and humidity_pct > 0 else None,
            "humidity_level": _int(unit.get("humidity")),        # 1 (sec) à 5 (humide)
            "temp": _int(unit.get("temp")),
            "can_dry": can_dry,                                  # l'AMS sait chauffer
            "remote_dry": can_dry and remote_dry,                # …et le firmware accepte l'ordre à distance
            "dry_blocked": blocked if can_dry else [],           # raisons données par l'imprimante
            "dry_max_temp": DRYING_MAX_TEMP.get(model),
            "drying": dry_left > 0,
            "dry_remaining_min": dry_left,
            "dry_temp": _int(dry_setting.get("dry_temperature")),
            "dry_filament": dry_setting.get("dry_filament") or None,
            "slots": [s for s in slots if s["ams_id"] == str(ams_id)],
        })
    return units


def external_spool_from_raw(raw: dict) -> dict | None:
    """Bobine sur le support externe (« vt_tray »), si l'imprimante la décrit."""
    tray = raw.get("print", {}).get("vt_tray")
    if not isinstance(tray, dict):
        return None
    tray_type = tray.get("tray_type") or ""
    return {"type": tray_type or None, "color": tray.get("tray_color") or None, "empty": tray_type == ""}


def light_state_from_raw(raw: dict) -> str | None:
    """'on' / 'off' de la lumière de chambre (rapport `lights_report`)."""
    for light in raw.get("print", {}).get("lights_report", []) or []:
        if light.get("node") == "chamber_light":
            return light.get("mode")
    return None


def fan_percent_from_raw(raw: dict, key: str) -> int | None:
    """Vitesse de ventilateur en % : l'imprimante envoie un palier de 0 à 15."""
    value = raw.get("print", {}).get(key)
    try:
        return round(int(value) / 15 * 100)
    except (TypeError, ValueError):
        return None


def _state_name(printer) -> str | None:
    """'RUNNING', 'PAUSE', 'IDLE'… (valeur de l'enum GcodeState de la lib)."""
    state = _safe(printer.get_state)
    if state is None:
        return None
    return getattr(state, "value", str(state))


# ── Une imprimante ────────────────────────────────────────
class PrinterWorker:
    """Gère une imprimante : connexion, threads de fond, état partagé."""

    def __init__(self, printer_cfg: dict, history: "HistoryDB"):
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
        self.fans = models.fans(self.model)

        self.history = history
        self.jobs = history_module.JobTracker(history, self.id)
        self.lock = threading.Lock()
        self.printer = None
        self.connection = CONNECTING
        self.last_error = None
        self.status = {}
        # Caméra gérée par nous (pas par bambulabs_api) : ouverte à la demande
        self.camera = camera.create_camera(models.camera_protocol(self.model), self.ip, self.access_code)
        self.sd = sd_card.SdCard(self.ip, self.access_code)
        self._sd_lock = threading.Lock()   # une seule opération FTP à la fois
        self._last_message_at = 0.0
        self._stop_event = threading.Event()
        self._command_lock = threading.Lock()
        self._replies = {}               # commande -> (heure, résultat, raison)
        self.ams_models = {}             # {ams_id: "AMS 2 Pro"…} d'après la liste des modules
        self._ams_raw = {}               # état AMS reconstitué message après message (voir merge_ams)
        self._vt_tray_raw = {}
        self.command_health = "unknown"  # "ok" / "ignored" : les commandes sont-elles appliquées ?

    # ── Lecture de l'état partagé (appelé par Flask) ──
    @property
    def connected(self) -> bool:
        return self.connection == ONLINE

    def snapshot(self) -> dict:
        with self.lock:
            data = dict(self.status)
        data["has_chamber_sensor"] = self.has_chamber_sensor
        data["printer_name"] = self.name
        data["model"] = self.model
        data["camera"] = self.camera_status()
        data["camera_available"] = data["camera"]["state"] == camera.LIVE
        data["fans_available"] = self.fans
        data["command_health"] = self.command_health
        data["job"] = self.jobs.current()
        data["print_error"] = history_module.format_error_code(data.get("print_error_code"))
        return data

    def connection_info(self) -> dict:
        with self.lock:
            return {"connection": self.connection, "connected": self.connection == ONLINE,
                    "last_error": self.last_error}

    def get_camera(self):
        """Dernière image JPEG (ou None) ; signale qu'un spectateur regarde."""
        if self.connected:
            self.camera.touch()
        return self.camera.latest()

    def camera_status(self) -> dict:
        if not self.connected:
            return {"state": camera.UNAVAILABLE, "message": "Imprimante hors ligne",
                    "frame_id": 0, "protocol": self.camera.protocol}
        status = self.camera.status()
        if status["state"] == camera.STOPPED:
            status["message"] = "Caméra en veille"
        return status

    def get_history(self, limit=200):
        return self.history.recent(self.id, limit)

    # ── Boucle de fond ──
    def _set_connection(self, state: str, error: str | None = None):
        with self.lock:
            if self.connection != state:
                log.info("[%s] %s → %s%s", self.id, self.connection, state,
                         f" ({error})" if error else "")
            self.connection = state
            self.last_error = error
            if state != ONLINE:
                self.printer = None

    def _on_mqtt_message(self, _mqtt=None, _client=None, _userdata=None, msg=None):
        self._last_message_at = time.time()
        if msg is not None:
            self._record_reply(msg.payload)

    def _record_reply(self, payload: bytes):
        """Garde la dernière réponse de l'imprimante pour chaque commande envoyée."""
        try:
            doc = json.loads(payload)
        except (ValueError, TypeError):
            return
        report = doc.get("print") if isinstance(doc, dict) else None
        if isinstance(report, dict):
            with self.lock:
                if isinstance(report.get("ams"), dict):
                    self._ams_raw = merge_ams(self._ams_raw, report["ams"])
                if isinstance(report.get("vt_tray"), dict):
                    self._vt_tray_raw = {**self._vt_tray_raw, **report["vt_tray"]}
        info = doc.get("info") if isinstance(doc, dict) else None
        if isinstance(info, dict) and info.get("command") == "get_version":
            found = ams_models_from_modules(info.get("module"))
            if found:
                with self.lock:
                    self.ams_models = found
        for section in doc.values():
            if isinstance(section, dict) and "command" in section and "result" in section:
                with self.lock:
                    self._replies[section["command"]] = (
                        time.time(), str(section["result"]).lower(), section.get("reason") or "")

    def _read_status(self, printer, raw: dict) -> dict:
        mqtt = printer.mqtt_client
        with self.lock:
            ams_raw, vt_raw = dict(self._ams_raw), dict(self._vt_tray_raw)
        if ams_raw or vt_raw:   # nos versions fusionnées remplacent celles (incomplètes) de la lib
            raw = {**raw, "print": {**raw.get("print", {}),
                                    **({"ams": ams_raw} if ams_raw else {}),
                                    **({"vt_tray": vt_raw} if vt_raw else {})}}
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "gcode_state": _state_name(printer),
            "file_name": _safe(printer.get_file_name),
            "mc_percent": _safe(printer.get_percentage),
            "remaining_time_min": _safe(printer.get_time),
            "current_layer": _safe(printer.current_layer_num),
            "total_layers": _safe(printer.total_layer_num),
            "nozzle_temp": _safe(printer.get_nozzle_temperature),
            "nozzle_target": _safe(mqtt.get_nozzle_temperature_target),
            "bed_temp": _safe(printer.get_bed_temperature),
            "bed_target": _safe(mqtt.get_bed_temperature_target),
            "chamber_temp": _safe(printer.get_chamber_temperature)
                if self.has_chamber_sensor else None,
            "print_speed": _safe(printer.get_print_speed),
            "wifi_signal": _safe(printer.wifi_signal),
            "light_state": light_state_from_raw(raw),
            "fan_part_pct": fan_percent_from_raw(raw, "cooling_fan_speed"),
            "fan_aux_pct": fan_percent_from_raw(raw, "big_fan1_speed"),
            "fan_chamber_pct": fan_percent_from_raw(raw, "big_fan2_speed"),
            "print_error_code": _safe(printer.print_error_code),
            "ams": extract_ams_from_raw(raw),
            "ams_units": extract_ams_units(raw, self.ams_models, self.model),
            "external_spool": external_spool_from_raw(raw),
        }

    def _manage_camera(self):
        """Coupe la caméra quand plus personne ne regarde depuis un moment."""
        if self.camera.running() and self.camera.idle():
            log.info("[%s] Plus de spectateur : caméra mise en veille", self.id)
            self.camera.stop()

    def _watch(self, printer):
        """Surveille une connexion ouverte ; rend la main si l'imprimante ne répond plus."""
        started = time.time()
        last_db_write = 0.0
        version_asked = False
        while not self._stop_event.is_set():
            now = time.time()
            if self._last_message_at and not version_asked:
                # Liste des modules : permet de reconnaître AMS 2 Pro / AMS HT / AMS lite
                version_asked = True
                _safe(lambda: commands.publish(printer, {"info": {"sequence_id": "0", "command": "get_version"}}))
            if self._last_message_at == 0:
                if now - started > config.FIRST_REPORT_TIMEOUT_SECONDS:
                    if printer.mqtt_client_connected():
                        raise RuntimeError("Connectée mais aucune donnée reçue : vérifie le numéro de série.")
                    raise RuntimeError("Imprimante injoignable : vérifie l'IP, l'access code et le mode LAN.")
            elif now - self._last_message_at > config.OFFLINE_AFTER_SECONDS:
                raise RuntimeError("Plus de réponse de l'imprimante.")
            else:
                raw = _safe(printer.mqtt_dump, {}) or {}
                data = self._read_status(printer, raw)
                self._manage_camera()
                with self.lock:
                    self.status = data
                    self.printer = printer
                self._set_connection(ONLINE)
                last_db_write = self._record_history(data, now, last_db_write)
            self._stop_event.wait(config.STATUS_POLL_INTERVAL)

    def _record_history(self, data: dict, now: float, last_db_write: float) -> float:
        """Suit les impressions et enregistre un relevé (30 s en impression, 5 min au repos)."""
        try:
            self.jobs.update(data)
            printing = data.get("gcode_state") in history_module.ACTIVE_STATES
            interval = config.HISTORY_SAVE_INTERVAL if printing else history_module.IDLE_SNAPSHOT_SECONDS
            if now - last_db_write < interval:
                return last_db_write
            self.history.add_snapshot(self.id, data, job_id=self.jobs.job_id)
            self.jobs.save_progress(data)
        except sqlite3.Error as e:
            log.warning("[%s] Erreur SQLite : %s", self.id, e)
        return now

    @staticmethod
    def _shutdown(printer):
        """Ferme MQTT + caméra en arrière-plan (la caméra peut mettre du temps à rendre la main)."""
        def close():
            _safe(lambda: printer.mqtt_client._client.disconnect())
            _safe(printer.mqtt_stop)
        threading.Thread(target=close, daemon=True).start()

    def run(self):
        while not self._stop_event.is_set():
            self._set_connection(CONNECTING)
            self._last_message_at = 0.0
            with self.lock:
                self._ams_raw, self._vt_tray_raw = {}, {}
            printer = bl.Printer(self.ip, self.access_code, self.serial)
            printer.mqtt_client.on_message_handler = self._on_mqtt_message
            printer.mqtt_client.pushall_timeout = 20  # redemande l'état complet régulièrement
            try:
                log.info("[%s] Connexion à %s…", self.id, self.ip)
                printer.mqtt_start()  # MQTT seulement : la caméra est gérée par camera.py
                self._watch(printer)
            except Exception as e:
                self._set_connection(OFFLINE, str(e))
                log.warning("[%s] %s Nouvelle tentative dans %ss.",
                            self.id, e, config.RECONNECT_DELAY_SECONDS)
            finally:
                self.camera.stop()
                self._shutdown(printer)
            self._stop_event.wait(config.RECONNECT_DELAY_SECONDS)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name=f"printer-{self.id}").start()

    def stop(self):
        self._stop_event.set()
        self.camera.stop()

    # ── Upload ──
    # ── Carte SD (lèvent sd_card.SdCardError ou RuntimeError avec un message clair) ──
    def upload(self, filepath: Path) -> str:
        self._require_printer()
        with self._sd_lock:
            return self.sd.upload(filepath)

    def list_sd_files(self) -> list[dict]:
        self._require_printer()
        with self._sd_lock:
            return self.sd.list_files()

    def download_sd_file(self, name: str, destination: Path):
        self._require_printer()
        with self._sd_lock:
            self.sd.download(name, destination)

    def sd_list_dir(self, folder: str) -> list[dict]:
        self._require_printer()
        with self._sd_lock:
            return self.sd.list_dir(folder)

    def sd_read_small(self, path: str) -> bytes:
        self._require_printer()
        with self._sd_lock:
            return self.sd.read_small(path)

    def sd_download_path(self, path: str, destination: Path):
        self._require_printer()
        with self._sd_lock:
            self.sd.download_path(path, destination)

    def delete_sd_file(self, name: str):
        self._require_printer()
        status = self._current_status()
        current_file = status.get("file_name") or ""
        if status.get("gcode_state") in history_module.ACTIVE_STATES and current_file                 and name.startswith(current_file):
            raise RuntimeError("Ce fichier est en cours d'impression.")
        with self._sd_lock:
            self.sd.delete(name)

    # ── Commandes ──
    def _require_printer(self):
        with self.lock:
            printer = self.printer
            online = self.connection == ONLINE
        if not printer or not online:
            raise RuntimeError("Imprimante non connectée")
        return printer

    def _reply_since(self, names: tuple, since: float):
        with self.lock:
            for name in names:
                reply = self._replies.get(name)
                if reply and reply[0] >= since:
                    return reply
        return None

    def _current_status(self) -> dict:
        with self.lock:
            return dict(self.status)

    def execute(self, name: str, params: dict) -> commands.CommandResult:
        """Envoie une commande et attend que l'imprimante confirme son effet."""
        spec = commands.COMMANDS[name]
        if name == "start":
            params = {**params, "model": self.model}   # le format du lancement dépend du modèle
        what = commands.describe(name, params)
        Result = commands.CommandResult

        try:
            printer = self._require_printer()
        except RuntimeError as e:
            return Result(commands.REJECTED, f"{what} impossible : {e}.")
        if not self._command_lock.acquire(blocking=False):
            return Result(commands.REJECTED, "Une autre commande est en cours, patiente quelques secondes.")
        try:
            status = self._current_status()
            if spec.precondition:
                problem = spec.precondition(status, params)
                if problem:
                    return Result(commands.REJECTED, problem)
            if spec.is_done(status, params):
                return Result(commands.CONFIRMED, f"{what} : déjà le cas.")

            sent_at = time.time()
            try:
                sent = spec.send(printer, params)
            except Exception as e:
                log.warning("[%s] Envoi %s échoué : %s", self.id, name, e)
                return Result(commands.ERROR, f"{what} : envoi impossible ({e}).")
            if not sent:
                return Result(commands.ERROR, f"{what} : envoi impossible (connexion MQTT perdue).")
            if name == "stop":
                self.jobs.stop_requested = True  # l'historique notera « arrêtée » et non « échec »
            commands.request_full_status(printer)
            return self._wait_for_confirmation(spec, params, what, sent_at)
        finally:
            self._command_lock.release()

    def _wait_for_confirmation(self, spec, params, what, sent_at):
        Result = commands.CommandResult
        deadline = sent_at + spec.timeout
        while time.time() < deadline and not self._stop_event.is_set():
            reply = self._reply_since(spec.reply_commands, sent_at)
            if reply and reply[1] not in ("success", "ok"):
                self.command_health = "ignored"
                reason = f" ({reply[2]})" if reply[2] else ""
                return Result(commands.REJECTED,
                              f"{what} : refusée par l'imprimante{reason}. {commands.DEV_MODE_HINT}")
            if spec.is_done(self._current_status(), params):
                self.command_health = "ok"
                log.info("[%s] %s confirmée", self.id, what)
                return Result(commands.CONFIRMED, f"{what} : confirmée par l'imprimante.")
            time.sleep(0.25)
        self.command_health = "ignored"
        log.warning("[%s] %s non confirmée après %ss", self.id, what, spec.timeout)
        return Result(commands.UNCONFIRMED,
                      f"{what} : envoyée, mais l'imprimante n'a pas confirmé en {int(spec.timeout)} s. "
                      f"{commands.DEV_MODE_HINT}")


# ── Toutes les imprimantes ────────────────────────────────
class PrinterManager:
    """Registre de tous les PrinterWorker actifs, indexés par printer_id."""

    def __init__(self, history: "HistoryDB | None" = None):
        self._history = history
        self.workers = {}
        self._lock = threading.Lock()

    @property
    def history(self) -> "HistoryDB":
        # Ouverture paresseuse : importer ce module ne crée pas de base.
        if self._history is None:
            self._history = HistoryDB(config.DB_PATH)
        return self._history

    def load_from_store(self, printer_configs: list):
        for cfg in printer_configs:
            if cfg["id"] not in self.workers:
                self.add_and_start(cfg)

    def add_and_start(self, printer_cfg: dict):
        with self._lock:
            old = self.workers.pop(printer_cfg["id"], None)
            if old:
                old.stop()
            worker = PrinterWorker(printer_cfg, self.history)
            self.workers[printer_cfg["id"]] = worker
        worker.start()

    def restart(self, printer_cfg: dict):
        """Après modification d'une imprimante : on relance sa connexion."""
        self.add_and_start(printer_cfg)

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
