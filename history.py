"""
Historique SQLite : relevés réguliers, impressions et erreurs.

Trois tables :
- snapshots : températures / progression, toutes les 30 s pendant une
              impression, toutes les 5 min au repos (moins d'écritures sur la
              carte SD du Raspberry Pi)
- jobs      : une ligne par impression (début, fin, durée, statut, filament)
- errors    : codes d'erreur remontés par l'imprimante

La base n'est écrite qu'à ces moments-là (jamais toutes les secondes) et
n'est lue que lorsqu'une page d'historique est affichée.
"""
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("history")

SNAPSHOT_COLUMNS = (
    "timestamp", "gcode_state", "file_name", "mc_percent", "remaining_time_min",
    "current_layer", "total_layers", "nozzle_temp", "bed_temp", "chamber_temp",
    "print_speed", "wifi_signal", "light_state",
)
IDLE_SNAPSHOT_SECONDS = 300
RETENTION_DAYS = 90
MAX_CHART_POINTS = 400

ACTIVE_STATES = ("PREPARE", "RUNNING", "PAUSE")
JOB_STATUS_LABELS = {
    "running": "En cours", "finished": "Terminée", "failed": "Échec",
    "stopped": "Arrêtée", "interrupted": "Interrompue",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_error_code(code) -> str | None:
    """50348044 → '0300-400C' (format affiché par Bambu Studio / le wiki Bambu)."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    hex_code = f"{value:08X}"
    return f"{hex_code[:4]}-{hex_code[4:]}"


class HistoryDB:
    """Une connexion SQLite partagée, protégée par un verrou (plusieurs threads l'utilisent)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")   # lectures et écritures en parallèle
        self.conn.execute("PRAGMA synchronous=NORMAL")  # moins d'écritures sur la carte SD
        self._create_schema()
        self._last_purge = 0.0

    # ── Schéma et migrations ──
    def _create_schema(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    gcode_state TEXT, file_name TEXT, mc_percent INTEGER,
                    remaining_time_min INTEGER, current_layer INTEGER, total_layers INTEGER,
                    nozzle_temp REAL, bed_temp REAL, chamber_temp REAL,
                    print_speed INTEGER, wifi_signal TEXT, light_state TEXT,
                    raw_json TEXT  -- plus rempli (trop volumineux), gardé pour compatibilité
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id TEXT NOT NULL,
                    file_name TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    duration_s INTEGER,
                    last_percent INTEGER,
                    last_layer INTEGER,
                    total_layers INTEGER,
                    error_code TEXT,
                    ams_start_json TEXT,
                    filament_json TEXT
                );
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id TEXT NOT NULL,
                    job_id INTEGER,
                    timestamp TEXT NOT NULL,
                    code TEXT NOT NULL,
                    gcode_state TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_printer ON jobs(printer_id, id);
                CREATE INDEX IF NOT EXISTS idx_errors_printer ON errors(printer_id, id);
                CREATE TABLE IF NOT EXISTS ai_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id TEXT,
                    job_id INTEGER,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,          -- alert, finish, plate, filament, summary, queue, info
                    level TEXT NOT NULL,         -- info, warn, alert
                    title TEXT NOT NULL,
                    message TEXT,
                    confidence INTEGER,
                    photo TEXT,                  -- nom du fichier dans data/photos
                    read INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_ai_events ON ai_events(id);
            """)
            job_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
            for column in ("ai_comment", "photo"):   # ajoutées avec les fonctions IA
                if column not in job_columns:
                    self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(snapshots)")}
            if "job_id" not in columns:  # base créée par une ancienne version
                self.conn.execute("ALTER TABLE snapshots ADD COLUMN job_id INTEGER")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_printer ON snapshots(printer_id, id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_job ON snapshots(job_id)")
            self.conn.commit()
        self._drop_legacy_raw_json()

    def _drop_legacy_raw_json(self):
        """Les anciennes versions stockaient le JSON MQTT complet : on libère cette place une fois."""
        with self.lock:
            heavy = self.conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE raw_json IS NOT NULL").fetchone()[0]
            if not heavy:
                return
            log.info("Nettoyage de %s anciens relevés volumineux (une seule fois)…", heavy)
            self.conn.execute("UPDATE snapshots SET raw_json = NULL")
            self.conn.commit()
            self.conn.execute("VACUUM")

    # ── Écritures ──
    def add_snapshot(self, printer_id: str, data: dict, job_id: int | None = None):
        values = [data.get(col) for col in SNAPSHOT_COLUMNS]
        values[SNAPSHOT_COLUMNS.index("wifi_signal")] = str(data.get("wifi_signal") or "")
        columns = ("printer_id", "job_id") + SNAPSHOT_COLUMNS
        with self.lock:
            self.conn.execute(
                f"INSERT INTO snapshots ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                [printer_id, job_id] + values,
            )
            self.conn.commit()
        self._purge_if_due()

    def _purge_if_due(self):
        """Supprime les relevés de plus de RETENTION_DAYS jours (au plus une fois par jour)."""
        if time.time() - self._last_purge < 86400:
            return
        self._last_purge = time.time()
        cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
        with self.lock:
            deleted = self.conn.execute(
                "DELETE FROM snapshots WHERE timestamp < ?", (cutoff,)).rowcount
            self.conn.commit()
        if deleted:
            log.info("%s relevés de plus de %s jours supprimés", deleted, RETENTION_DAYS)

    def start_job(self, printer_id: str, file_name: str | None, data: dict, ams: list) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO jobs (printer_id, file_name, started_at, status, last_percent, "
                "last_layer, total_layers, ams_start_json) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
                (printer_id, file_name, now_iso(), data.get("mc_percent"),
                 data.get("current_layer"), data.get("total_layers"), json.dumps(ams)),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_job_progress(self, job_id: int, data: dict):
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET last_percent = ?, last_layer = ?, total_layers = ? WHERE id = ?",
                (data.get("mc_percent"), data.get("current_layer"), data.get("total_layers"), job_id),
            )
            self.conn.commit()

    def finish_job(self, job_id: int, status: str, data: dict, filament: list,
                   error_code: str | None = None):
        ended = datetime.now()
        with self.lock:
            row = self.conn.execute("SELECT started_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
            duration = None
            if row:
                duration = int((ended - datetime.fromisoformat(row["started_at"])).total_seconds())
            self.conn.execute(
                "UPDATE jobs SET ended_at = ?, status = ?, duration_s = ?, last_percent = "
                "COALESCE(?, last_percent), last_layer = COALESCE(?, last_layer), "
                "error_code = COALESCE(?, error_code), filament_json = ? WHERE id = ?",
                (ended.isoformat(timespec="seconds"), status, duration, data.get("mc_percent"),
                 data.get("current_layer"), error_code, json.dumps(filament), job_id),
            )
            self.conn.commit()

    def add_error(self, printer_id: str, code: str, job_id: int | None, gcode_state: str | None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO errors (printer_id, job_id, timestamp, code, gcode_state) "
                "VALUES (?, ?, ?, ?, ?)", (printer_id, job_id, now_iso(), code, gcode_state))
            if job_id:
                self.conn.execute("UPDATE jobs SET error_code = ? WHERE id = ?", (code, job_id))
            self.conn.commit()

    # ── Lectures ──
    # ── Événements IA (alertes, résumés…) ──
    def add_event(self, kind: str, level: str, title: str, message: str = "", printer_id: str | None = None,
                  job_id: int | None = None, confidence: int | None = None, photo: str | None = None,
                  read: bool = False) -> int:
        """read=True : événement de routine qui ne fait pas monter le compteur d'alertes."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO ai_events (printer_id, job_id, timestamp, kind, level, title, message, confidence, "
                "photo, read) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (printer_id, job_id, now_iso(), kind, level, title, message, confidence, photo, int(read)))
            self.conn.commit()
            return cur.lastrowid

    def delete_events(self, event_id: int | None = None) -> list[str]:
        """
        Supprime une alerte (ou toutes si event_id est None).
        Renvoie les photos qui ne servent plus nulle part (ni autre alerte, ni historique) :
        on peut les effacer du disque.
        """
        with self.lock:
            where, args = ("WHERE id = ?", (event_id,)) if event_id is not None else ("", ())
            photos = {r[0] for r in self.conn.execute(f"SELECT photo FROM ai_events {where}", args) if r[0]}
            self.conn.execute(f"DELETE FROM ai_events {where}", args)
            self.conn.commit()
            still_used = {r[0] for r in self.conn.execute(
                "SELECT photo FROM ai_events WHERE photo IS NOT NULL "
                "UNION SELECT photo FROM jobs WHERE photo IS NOT NULL")}
        return sorted(photos - still_used)

    def clear(self, printer_id: str | None = None) -> tuple[dict, list[str]]:
        """
        Efface l'historique d'une imprimante (ou de toutes si printer_id est None) :
        impressions, relevés (courbes) et erreurs. L'impression en cours est gardée.
        Renvoie (nombre de lignes effacées, photos de fin d'impression devenues inutiles).
        """
        printer_filter, args = ("AND printer_id = ?", (printer_id,)) if printer_id else ("", ())
        with self.lock:
            running = [r[0] for r in self.conn.execute(
                f"SELECT id FROM jobs WHERE status = 'running' {printer_filter}", args)]
            keep = f"AND (job_id IS NULL OR job_id NOT IN ({','.join('?' * len(running))}))" if running else ""
            photos = {r[0] for r in self.conn.execute(
                f"SELECT photo FROM jobs WHERE status != 'running' AND photo IS NOT NULL {printer_filter}", args)}
            counts = {
                "jobs": self.conn.execute(
                    f"DELETE FROM jobs WHERE status != 'running' {printer_filter}", args).rowcount,
                "snapshots": self.conn.execute(
                    f"DELETE FROM snapshots WHERE 1 = 1 {printer_filter} {keep}", args + tuple(running)).rowcount,
                "errors": self.conn.execute(
                    f"DELETE FROM errors WHERE 1 = 1 {printer_filter} {keep}", args + tuple(running)).rowcount,
            }
            self.conn.commit()
            still_used = {r[0] for r in self.conn.execute(
                "SELECT photo FROM ai_events WHERE photo IS NOT NULL "
                "UNION SELECT photo FROM jobs WHERE photo IS NOT NULL")}
        return counts, sorted(photos - still_used)

    def forget_photos(self, names: list[str]):
        """Photos supprimées du disque : on retire le lien dans les alertes."""
        with self.lock:
            self.conn.executemany("UPDATE ai_events SET photo = NULL WHERE photo = ?", [(n,) for n in names])
            self.conn.commit()

    def events(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        return self._rows("SELECT * FROM ai_events ORDER BY id DESC LIMIT ?", (limit,))

    def unread_events(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM ai_events WHERE read = 0").fetchone()[0]

    def mark_events_read(self):
        with self.lock:
            self.conn.execute("UPDATE ai_events SET read = 1 WHERE read = 0")
            self.conn.commit()

    def set_job_ai(self, job_id: int, comment: str, photo: str | None):
        with self.lock:
            self.conn.execute("UPDATE jobs SET ai_comment = ?, photo = ? WHERE id = ?", (comment, photo, job_id))
            self.conn.commit()

    def job(self, job_id: int) -> dict | None:
        rows = self.jobs_by_ids([job_id])
        return rows[0] if rows else None

    def jobs_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        rows = self._rows(f"SELECT * FROM jobs WHERE id IN ({marks})", ids)
        for row in rows:
            row["filament"] = json.loads(row.pop("filament_json") or "[]")
            row["ams_start"] = json.loads(row.pop("ams_start_json") or "[]")
        return rows

    def _rows(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def recent(self, printer_id: str, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        rows = self._rows(
            f"SELECT id, {', '.join(SNAPSHOT_COLUMNS)} FROM snapshots "
            "WHERE printer_id = ? ORDER BY id DESC LIMIT ?", (printer_id, limit))
        return list(reversed(rows))

    def running_job(self, printer_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM jobs WHERE printer_id = ? AND status = 'running' "
                          "ORDER BY id DESC LIMIT 1", (printer_id,))
        return rows[0] if rows else None

    def jobs(self, printer_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        where, params = ("WHERE printer_id = ?", [printer_id]) if printer_id else ("", [])
        rows = self._rows(f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", params + [limit])
        for row in rows:
            row["filament"] = json.loads(row.pop("filament_json") or "[]")
            row.pop("ams_start_json", None)
            row["status_label"] = JOB_STATUS_LABELS.get(row["status"], row["status"])
        return rows

    def errors(self, printer_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        where, params = ("WHERE e.printer_id = ?", [printer_id]) if printer_id else ("", [])
        return self._rows(
            f"SELECT e.*, j.file_name FROM errors e LEFT JOIN jobs j ON j.id = e.job_id "
            f"{where} ORDER BY e.id DESC LIMIT ?", params + [limit])

    def samples(self, printer_id: str, job_id: int | None = None, hours: float = 24) -> list[dict]:
        """Relevés pour les graphiques, réduits à MAX_CHART_POINTS points au maximum."""
        columns = "timestamp, nozzle_temp, bed_temp, chamber_temp, mc_percent, gcode_state"
        if job_id:
            rows = self._rows(f"SELECT {columns} FROM snapshots WHERE printer_id = ? AND job_id = ? "
                              "ORDER BY id", (printer_id, job_id))
        else:
            since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
            rows = self._rows(f"SELECT {columns} FROM snapshots WHERE printer_id = ? AND timestamp >= ? "
                              "ORDER BY id", (printer_id, since))
        step = max(1, -(-len(rows) // MAX_CHART_POINTS))  # division arrondie au supérieur
        return rows[::step]

    def stats(self, printer_id: str | None = None, days: int = 30) -> dict:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        where = "WHERE started_at >= ?" + (" AND printer_id = ?" if printer_id else "")
        params = [since] + ([printer_id] if printer_id else [])
        rows = self._rows(f"SELECT status, COUNT(*) AS n, COALESCE(SUM(duration_s), 0) AS seconds "
                          f"FROM jobs {where} GROUP BY status", params)
        by_status = {r["status"]: r["n"] for r in rows}
        finished_jobs = sum(n for s, n in by_status.items() if s != "running")
        return {
            "days": days,
            "jobs": sum(by_status.values()),
            "finished": by_status.get("finished", 0),
            "failed": by_status.get("failed", 0) + by_status.get("interrupted", 0),
            "stopped": by_status.get("stopped", 0),
            "print_seconds": sum(r["seconds"] for r in rows),
            "success_rate": round(100 * by_status.get("finished", 0) / finished_jobs) if finished_jobs else None,
        }


def filament_used(ams_start: list, ams_end: list) -> list[dict]:
    """
    Estimation du filament consommé, en % de bobine, par emplacement AMS.
    Ne marche qu'avec les bobines Bambu Lab (les seules dont l'imprimante
    connaît le niveau restant).
    """
    start_by_slot = {(s["ams_id"], s["tray_id"]): s for s in ams_start or []}
    used = []
    for slot in ams_end or []:
        before = start_by_slot.get((slot["ams_id"], slot["tray_id"]))
        if not before or before.get("remaining") is None or slot.get("remaining") is None:
            continue
        diff = before["remaining"] - slot["remaining"]
        if diff > 0:
            used.append({"slot": f"{int(slot['ams_id']) + 1}-{int(slot['tray_id']) + 1}",
                         "type": slot.get("type"), "color": slot.get("color"), "used_pct": diff})
    return used


class JobTracker:
    """
    Suit l'état d'une imprimante et en déduit le début / la fin des impressions.
    Appelé à chaque lecture d'état (en mémoire) ; n'écrit en base qu'aux
    changements (début, fin, erreur).
    """

    def __init__(self, db: HistoryDB, printer_id: str):
        self.db = db
        self.printer_id = printer_id
        self.job_id = None
        self.file_name = None
        self.ams_start = []
        self.started_at = None
        self.last_error = None
        self.stop_requested = False
        self._checked_db = False

    def update(self, data: dict):
        state = data.get("gcode_state")
        if state in (None, "UNKNOWN"):
            return
        if not self._checked_db:
            self._resume_from_db(data)
        if state in ACTIVE_STATES:
            if self.job_id and data.get("file_name") and data.get("file_name") != self.file_name:
                self._end("interrupted", data)   # un autre fichier a démarré entre-temps
            if not self.job_id:
                self._start(data)
        # L'erreur est enregistrée tant que l'impression est encore suivie, pour y être rattachée
        self._record_error(data)
        if state not in ACTIVE_STATES and self.job_id:
            self._end(self._final_status(state), data)

    def current(self) -> dict | None:
        """Impression suivie en ce moment (pour afficher le temps écoulé)."""
        if not self.job_id:
            return None
        return {"id": self.job_id, "file_name": self.file_name, "started_at": self.started_at}

    def save_progress(self, data: dict):
        if self.job_id:
            self.db.update_job_progress(self.job_id, data)

    def _resume_from_db(self, data: dict):
        """Après un redémarrage de l'appli : reprend l'impression en cours ou la clôture."""
        self._checked_db = True
        job = self.db.running_job(self.printer_id)
        if not job:
            return
        if data.get("gcode_state") in ACTIVE_STATES and data.get("file_name") == job["file_name"]:
            self.job_id, self.file_name = job["id"], job["file_name"]
            self.started_at = job["started_at"]
            self.ams_start = json.loads(job["ams_start_json"] or "[]")
        else:
            self.db.finish_job(job["id"], "interrupted", {}, [])

    def _start(self, data: dict):
        self.file_name = data.get("file_name")
        self.ams_start = data.get("ams") or []
        self.stop_requested = False
        self.started_at = now_iso()
        self.job_id = self.db.start_job(self.printer_id, self.file_name, data, self.ams_start)
        log.info("[%s] Impression démarrée : %s (job %s)", self.printer_id, self.file_name, self.job_id)

    def _final_status(self, state: str) -> str:
        if self.stop_requested:
            return "stopped"
        return {"FINISH": "finished", "FAILED": "failed"}.get(state, "interrupted")

    def _end(self, status: str, data: dict):
        filament = filament_used(self.ams_start, data.get("ams") or [])
        self.db.finish_job(self.job_id, status, data, filament)
        log.info("[%s] Impression %s : %s", self.printer_id, status, self.file_name)
        self.job_id, self.file_name, self.ams_start, self.started_at = None, None, [], None
        self.stop_requested = False

    def _record_error(self, data: dict):
        code = format_error_code(data.get("print_error_code"))
        if code and code != self.last_error:
            self.db.add_error(self.printer_id, code, self.job_id, data.get("gcode_state"))
            log.warning("[%s] Erreur imprimante %s", self.printer_id, code)
        self.last_error = code
