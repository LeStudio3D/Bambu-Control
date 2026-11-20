"""
Surveillance automatique (thread de fond, un passage toutes les 15 s) :

- caméra analysée par l'IA pendant l'impression → alerte, pause si activée ;
  chaque contrôle (même sans problème) est noté dans les alertes avec sa photo
- fin d'impression → notification, photo + commentaire IA, lumière éteinte
- bobine qui risque de manquer avant la fin → alerte (sans IA)
- file d'impression → propose le fichier suivant, plateau vérifié par l'IA
- résumé quotidien → notification à l'heure choisie

Les appels IA peuvent être lents (jusqu'à plusieurs minutes sur un petit
ordinateur) : tout se passe ici, en arrière-plan, sans bloquer l'interface.
"""
import logging
import threading
import time
from datetime import datetime

import ai_client
import ai_features
import config
import history
import print_queue
import settings_store

log = logging.getLogger("ai_monitor")

PHOTOS_DIR = config.DATA_DIR / "photos"
TICK_SECONDS = 15
ALERT_REPEAT_SECONDS = 15 * 60
WATCH_PHOTO_DAYS = 3
ACTIVE_STATES = ("PREPARE", "RUNNING", "PAUSE")


def grab_frame(worker, wait_seconds: float = 10) -> bytes | None:
    """Dernière image de la caméra ; l'allume si besoin et attend la première image."""
    end = time.time() + wait_seconds
    while time.time() < end:
        jpeg, _ = worker.get_camera()
        if jpeg:
            return jpeg
        time.sleep(1)
    return None


def save_photo(jpeg: bytes, prefix: str) -> str:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    (PHOTOS_DIR / name).write_bytes(jpeg)
    return name


def save_watch_photo(jpeg: bytes, printer_id: str) -> str:
    """Photo d'un contrôle de routine : réduite (640 px) pour ne pas remplir la carte SD."""
    try:
        jpeg = ai_client.shrink_image(jpeg)
    except Exception:  # image illisible : on garde l'originale
        pass
    return save_photo(jpeg, f"suivi_{printer_id}")


def check_plate_now(worker) -> dict | None:
    """Vérifie que le plateau est vide (avant un lancement). None si l'IA ou la caméra ne sont pas dispo."""
    cfg = ai_features.get_config()
    if not (cfg["plate_check"] and ai_features.vision_ready(cfg)):
        return None
    frame = grab_frame(worker)
    if frame is None:
        return None
    return ai_features.check_plate(frame, cfg)


class PrinterState:
    """Ce que la surveillance retient pour une imprimante (en mémoire)."""

    def __init__(self):
        self.last_job = None
        self.last_watch = 0.0
        self.last_alert = 0.0
        self.watch = None               # dernier résultat affiché sur l'accueil
        self.filament_alerted_job = None
        self.proposal = None            # prochaine impression proposée (file)


class AiMonitor:
    def __init__(self, manager):
        self.manager = manager
        self.states: dict[str, PrinterState] = {}
        self._thread = None

    # ── Lecture par l'interface ──
    def status(self, printer_id: str) -> dict:
        state = self.states.get(printer_id)
        cfg = ai_features.get_config()
        return {
            "enabled": cfg["enabled"],
            "watching": bool(cfg["enabled"] and cfg["watch_enabled"] and cfg["vision_model"]),
            "watch": state.watch if state else None,
            "proposal": state.proposal if state else None,
        }

    def forget_proposal(self, printer_id: str):
        if printer_id in self.states:
            self.states[printer_id].proposal = None

    # ── Boucle ──
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="ai-monitor")
            self._thread.start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:   # la surveillance ne doit jamais s'arrêter
                log.exception("Surveillance IA : %s", e)
            time.sleep(TICK_SECONDS)

    def tick(self):
        cfg = ai_features.get_config()
        for printer_id, worker in list(self.manager.workers.items()):
            state = self.states.setdefault(printer_id, PrinterState())
            if not worker.connected:
                continue
            data = worker.snapshot()
            self._track_job_end(worker, data, cfg, state)
            self._check_filament(worker, data, cfg, state)
            self._watch(worker, data, cfg, state)
            self._propose_next(worker, data, cfg, state)
        self._daily_summary(cfg)

    @property
    def history(self):
        return self.manager.history

    # ── Fin d'impression ──
    def _track_job_end(self, worker, data, cfg, state):
        job = data.get("job")
        if state.last_job and not job:
            self._on_job_end(worker, state.last_job["id"], cfg)
        state.last_job = job

    def _on_job_end(self, worker, job_id, cfg):
        job = self.history.job(job_id)
        if not job:
            return
        name = job.get("file_name") or "impression"
        ok = job["status"] == "finished"
        if cfg["finish_light_off"] and ok:
            worker.execute("light", {"on": False})
        comment, photo = "", None
        if cfg["finish_photo"]:          # photo pour la galerie, même sans IA
            frame = grab_frame(worker, wait_seconds=25)
            if frame:
                photo = save_photo(frame, f"fin_{job_id}")
                if ai_features.vision_ready(cfg):
                    try:
                        comment = ai_features.comment_finished(frame, name, cfg)["comment"]
                    except ai_client.AiError as e:
                        comment = f"(analyse IA impossible : {e})"
                self.history.set_job_ai(job_id, comment, photo)
        label = history.JOB_STATUS_LABELS.get(job["status"], job["status"]).lower()
        title = f"Impression terminée : {name}" if ok else f"Impression {label} : {name}"
        self.history.add_event("finish", "info" if ok else "warn", title, comment, worker.id, job_id, photo=photo)

    # ── Bobine qui risque de manquer (estimation, sans IA) ──
    def _check_filament(self, worker, data, cfg, state):
        job = data.get("job")
        percent = data.get("mc_percent") or 0
        if not (cfg["filament_alert"] and job and data.get("gcode_state") == "RUNNING" and percent >= 5):
            return
        if state.filament_alerted_job == job["id"]:
            return
        start = {(s["ams_id"], s["tray_id"]): s for s in worker.jobs.ams_start or []}
        for slot in data.get("ams") or []:
            before = start.get((slot["ams_id"], slot["tray_id"]))
            if not (slot.get("active") and before and before.get("remaining") is not None
                    and slot.get("remaining") is not None):
                continue
            used = before["remaining"] - slot["remaining"]
            if used <= 0:
                continue
            still_needed = used * (100 - percent) / percent
            if still_needed > slot["remaining"]:
                state.filament_alerted_job = job["id"]
                self.history.add_event(
                    "filament", "warn", f"La bobine {slot['label']} ({slot.get('type') or '?'}) risque de manquer",
                    f"Il reste environ {slot['remaining']} % de bobine ; au rythme actuel, l'impression en "
                    f"demandera encore environ {round(still_needed)} %. Prévois une bobine de remplacement.",
                    worker.id, job["id"])

    # ── Surveillance caméra ──
    def _watch(self, worker, data, cfg, state):
        if not (cfg["enabled"] and cfg["watch_enabled"] and ai_features.vision_ready(cfg)):
            return
        if data.get("gcode_state") != "RUNNING" or (data.get("current_layer") or 0) < 2:
            return
        now = time.time()
        if now - state.last_watch < max(1, float(cfg["watch_interval_min"])) * 60:
            return
        state.last_watch = now
        self._check(worker, data, cfg, state, manual=False)

    def watch_now(self, worker) -> dict:
        """Contrôle demandé par l'utilisateur (bouton) : marche aussi hors impression, jamais de pause."""
        cfg = ai_features.get_config()
        if not ai_features.vision_ready(cfg):
            raise ai_client.AiError("Aucun modèle vision choisi (Paramètres → Intelligence artificielle).")
        if not worker.connected:
            raise ai_client.AiError("Imprimante hors ligne.")
        state = self.states.setdefault(worker.id, PrinterState())
        return self._check(worker, worker.snapshot(), cfg, state, manual=True)

    def _check(self, worker, data, cfg, state, manual: bool) -> dict:
        """Photo + analyse IA. Renvoie le résultat et l'enregistre dans les alertes."""
        now = time.time()
        job_id = (data.get("job") or {}).get("id")
        where = f"{data.get('mc_percent') or 0} %" if data.get("job") else "hors impression"
        prefix = "Contrôle manuel" if manual else "Contrôle caméra"
        log_it = manual or cfg["watch_log"]
        frame = grab_frame(worker, wait_seconds=25)
        if frame is None:
            error = "Caméra indisponible (aucune image reçue en 25 s)."
            state.watch = {"time": datetime.now().isoformat(timespec="seconds"), "error": error}
            if log_it:
                self.history.add_event("watch", "info", f"{prefix} impossible — {where}", error,
                                       worker.id, job_id, read=not manual)
            return {"error": error}
        printing = data.get("gcode_state") in ACTIVE_STATES
        try:
            if not printing:
                # Hors impression, chercher un « échec » n'a pas de sens : on décrit le plateau
                plate = ai_features.check_plate(frame, cfg)
                state_text = "plateau vide" if plate["clear"] else "objet(s) sur le plateau"
                self.history.add_event("watch", "ok" if plate["clear"] else "info",
                                       f"{prefix} : {state_text} — {where}", plate["description"], worker.id,
                                       job_id, plate["confidence"], save_watch_photo(frame, worker.id),
                                       read=True)
                self.prune_watch_photos()
                return plate
            result = ai_features.analyze_failure(frame, cfg)
        except ai_client.AiError as e:
            state.watch = {"time": datetime.now().isoformat(timespec="seconds"), "error": str(e)}
            if log_it:
                self.history.add_event("watch", "info", f"{prefix} impossible — {where}", str(e), worker.id,
                                       job_id, photo=save_watch_photo(frame, worker.id), read=not manual)
            return {"error": str(e)}
        if data.get("gcode_state") == "RUNNING":
            state.watch = {"time": datetime.now().isoformat(timespec="seconds"), **result}

        if not manual and result["failure"] and result["confidence"] >= int(cfg["pause_confidence"]) \
                and now - state.last_alert > ALERT_REPEAT_SECONDS:
            state.last_alert = now
            self._raise_failure_alert(worker, job_id, cfg, frame, result)
        elif log_it:
            # Contrôle enregistré même sans problème : photo + description de l'IA
            photo = save_watch_photo(frame, worker.id)
            if result["failure"]:
                note = (" — Contrôle manuel : aucune pause déclenchée." if manual
                        else " — Sous le seuil d'alerte, à surveiller."
                        if result["confidence"] < int(cfg["pause_confidence"])
                        else " — Alerte déjà envoyée il y a moins de 15 min.")
                self.history.add_event("watch", "warn", f"{prefix} : doute de l'IA ({result['confidence']} %) — {where}",
                                       result["description"] + note,
                                       worker.id, job_id, result["confidence"], photo)
            else:
                self.history.add_event("watch", "ok", f"{prefix} : RAS — {where}",
                                       result["description"], worker.id, job_id, result["confidence"], photo,
                                       read=True)
            self.prune_watch_photos()
        return result

    def _raise_failure_alert(self, worker, job_id, cfg, frame, result):
        photo = save_photo(frame, f"alerte_{worker.id}")
        message = result["description"]
        if cfg["auto_pause"]:
            outcome = worker.execute("pause", {})
            message += f" — Pause automatique : {outcome.message}"
        else:
            message += " — Vérifie l'impression (pause automatique désactivée)."
        self.history.add_event("alert", "alert", f"Échec possible détecté ({result['confidence']} %)",
                               message, worker.id, job_id, result["confidence"], photo)
        log.warning("[%s] Échec possible : %s", worker.id, message)

    def prune_watch_photos(self):
        """Les photos de contrôle « RAS » sont gardées WATCH_PHOTO_DAYS jours (les alertes, toujours)."""
        limit = time.time() - WATCH_PHOTO_DAYS * 86400
        removed = []
        for path in PHOTOS_DIR.glob("suivi_*.jpg"):
            if path.stat().st_mtime < limit:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        if removed:
            self.history.forget_photos(removed)

    # ── File d'impression ──
    def _propose_next(self, worker, data, cfg, state):
        queue = print_queue.items(worker.id)
        if not queue or data.get("gcode_state") not in ("IDLE", "FINISH"):
            state.proposal = None
            return
        first = queue[0]
        if state.proposal and state.proposal["name"] == first["name"]:
            return
        proposal = {**first, "created": datetime.now().isoformat(timespec="seconds"), "plate_check": None}
        try:
            proposal["plate_check"] = check_plate_now(worker)
        except ai_client.AiError as e:
            proposal["plate_check"] = {"error": str(e)}
        state.proposal = proposal
        check = proposal["plate_check"]
        detail = ("Plateau vide d'après l'IA." if check and check.get("clear")
                  else f"Plateau à vérifier : {check.get('description') or check.get('error')}" if check
                  else "Vérifie que le plateau est vide.")
        self.history.add_event("queue", "info", f"Prochaine impression prête : {first['name']}",
                               detail + " Confirme le lancement depuis l'accueil.", worker.id)

    # ── Résumé quotidien ──
    def _daily_summary(self, cfg):
        if not (cfg["enabled"] and cfg["daily_summary"]):
            return
        now = datetime.now()
        today = now.date().isoformat()
        try:
            hour, minute = map(int, str(cfg["daily_summary_time"]).split(":"))
        except ValueError:
            hour, minute = 8, 0
        if (now.hour, now.minute) < (hour, minute):
            return
        if settings_store.load().get("ai_last_summary") == today:
            return
        settings_store.save({"ai_last_summary": today})
        try:
            text = ai_features.summarize_history(self.history.stats(None, 1), self.history.jobs(None, 30),
                                                 self.history.errors(None, 20), 1, cfg)
        except ai_client.AiError as e:
            text = f"Résumé impossible : {e}"
        self.history.add_event("summary", "info", f"Résumé des dernières 24 h ({now.strftime('%d/%m')})", text)
