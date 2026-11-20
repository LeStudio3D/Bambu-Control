"""
Suivi « matériel » de chaque impression (thread de fond, sans IA) :

- au début : lit sur la carte SD le petit fichier /cache/<plateau>_<nom>.gcode.bbl
  (plateau, bobines utilisées, timelapse) puis le .3mf (grammes et prix au
  kilo de chaque filament, comme Bambu Studio) → coût prévu, vignette ;
- à la fin : coût réel (filament au prorata de l'avancement + électricité),
  décompte du stock de filament, alerte si une bobine est presque vide,
  récupération de la vidéo timelapse dans data/media/job_<id>/.

Marche aussi pour les impressions lancées depuis Bambu Studio ou l'écran.
"""
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
import settings_store
import spools as spools_module
import threemf

log = logging.getLogger("job_extras")

MEDIA_DIR = config.DATA_DIR / "media"
TICK_SECONDS = 10
TIMELAPSE_FOLDER = "/timelapse"
VIDEO_NAME = re.compile(r"^video_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.(mp4|avi|mkv)$", re.I)
BBL_NAME = re.compile(r"^(\d+)_(.+)\.gcode\.bbl$")
TIMELAPSE_MATCH_MINUTES = 10
TIMELAPSE_RETRY_SECONDS = 30      # la vidéo est écrite quelques instants après la fin
END_WAIT_SECONDS = 5

COST_DEFAULTS = {"electricity_price_kwh": 0.20, "printer_power_w": 100, "default_filament_price_kg": 20.0}


def cost_settings() -> dict:
    stored = settings_store.load().get("costs") or {}
    return {**COST_DEFAULTS, **{k: v for k, v in stored.items() if k in COST_DEFAULTS}}


def media_dir(job_id: int) -> Path:
    return MEDIA_DIR / f"job_{int(job_id)}"


def remove_media(job_ids: list[int]):
    for job_id in job_ids:
        shutil.rmtree(media_dir(job_id), ignore_errors=True)


def file_stem(name: str) -> str:
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def video_time(name: str) -> datetime | None:
    match = VIDEO_NAME.match(name)
    if not match:
        return None
    return datetime.fromisoformat(f"{match[1]}T{match[2]}:{match[3]}:{match[4]}")


def compute_costs(job: dict, settings: dict) -> dict:
    """Coût final d'une impression (filament au prorata de l'avancement si elle n'est pas finie)."""
    plan = job.get("plan") or {}
    ratio = 1.0 if job["status"] == "finished" else max(0, min(100, job.get("last_percent") or 0)) / 100
    hours = (job.get("duration_s") or 0) / 3600
    energy_kwh = round(hours * float(settings["printer_power_w"]) / 1000, 3)
    energy_cost = round(energy_kwh * float(settings["electricity_price_kwh"]), 2)
    result = {"energy_kwh": energy_kwh, "energy_cost": energy_cost,
              "filament_g": None, "filament_cost": None, "total_cost": None}
    if plan.get("filaments"):
        result["filament_g"] = round(sum((f.get("used_g") or 0) for f in plan["filaments"]) * ratio, 1)
        result["filament_cost"] = round((plan.get("filament_cost") or 0) * ratio, 2)
        result["total_cost"] = round(result["filament_cost"] + energy_cost, 2)
    return result


def usage_by_slot(plan: dict, ratio: float) -> dict[str, float]:
    usage: dict[str, float] = {}
    for f in (plan or {}).get("filaments") or []:
        if f.get("slot"):
            usage[f["slot"]] = usage.get(f["slot"], 0) + (f.get("used_g") or 0) * ratio
    return usage


class JobExtras:
    def __init__(self, manager, spool_store):
        self.manager = manager
        self.spools = spool_store
        self.current: dict[str, int | None] = {}
        self._busy: set = set()
        self._thread = None

    @property
    def history(self):
        return self.manager.history

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="job-extras")
            self._thread.start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:   # ne doit jamais s'arrêter
                log.exception("Suivi des impressions : %s", e)
            time.sleep(TICK_SECONDS)

    def _spawn(self, key, fn, *args):
        """Une seule tâche à la fois par (action, impression)."""
        if key in self._busy:
            return
        self._busy.add(key)

        def run():
            try:
                fn(*args)
            except Exception as e:
                log.warning("%s : %s", key, e)
            finally:
                self._busy.discard(key)
        threading.Thread(target=run, daemon=True).start()

    def tick(self):
        for printer_id, worker in list(self.manager.workers.items()):
            if not worker.connected:
                continue
            job = worker.jobs.current()
            job_id = job["id"] if job else None
            previous = self.current.get(printer_id, "unknown")
            if job_id and job_id != previous:
                self._spawn(("plan", job_id), self.prepare, worker, job_id)
            if previous not in ("unknown", None) and previous != job_id:
                self._spawn(("end", previous), self.finish, worker, previous)
            self.current[printer_id] = job_id

    # ── Début d'impression : ce qui est prévu ──
    def prepare(self, worker, job_id: int):
        job = self.history.job(job_id)
        if not job or job.get("plan") or not job.get("file_name"):
            return
        plan = self.read_plan(worker, job["file_name"])
        thumb = plan.pop("_thumbnail", None)
        fields = {"plate": plan.get("plate"), "plan_json": json.dumps(plan, ensure_ascii=False),
                  "timelapse_wanted": 1 if plan.get("timelapse") else 0}
        if thumb:
            folder = media_dir(job_id)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "plate.png").write_bytes(thumb)
            fields["thumbnail"] = "plate.png"
        self.history.update_job_fields(job_id, **fields)
        log.info("[%s] Impression %s : plateau %s, %s g prévus, %s € de filament", worker.id, job_id,
                 plan.get("plate"), plan.get("weight_g"), plan.get("filament_cost"))

    def read_plan(self, worker, file_name: str) -> dict:
        """Plateau, options et filaments prévus (bobine utilisée, grammes, prix)."""
        plan = {"source": "aucune", "plate": 1, "filaments": [], "weight_g": None, "filament_cost": None}
        bbl = self._find_bbl(worker, file_name)
        if bbl:
            plan.update(source="bbl", plate=bbl["plate"], use_ams=bbl.get("use ams", True),
                        timelapse=bool(bbl.get("timelapse")), bed_leveling=bbl.get("bed_leveling"),
                        flow_cali=bbl.get("flow_cali"))
        sd_path = self._sd_path(bbl.get("file path") if bbl else None, file_name)
        if not sd_path or not file_name.lower().endswith(".3mf"):
            return plan
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "job.3mf"
            try:
                worker.sd_download_path(sd_path, local)
                info = threemf.parse(local)
                cost = threemf.plate_cost(info, plan["plate"], float(cost_settings()["default_filament_price_kg"]))
                plan["_thumbnail"] = threemf.thumbnail(local, cost["plate"])
            except Exception as e:
                log.info("[%s] Fichier %s illisible pour le coût : %s", worker.id, sd_path, e)
                return plan
        mapping = bbl.get("ams mapping") if bbl else None
        ams_slots = worker.snapshot().get("ams") or []
        for f in cost["filaments"]:
            f["slot"] = self._slot_for(f, mapping, plan.get("use_ams", True), ams_slots)
        plan.update({k: cost[k] for k in ("plate", "filaments", "weight_g", "filament_cost", "prediction_s")})
        return plan

    def _find_bbl(self, worker, file_name: str) -> dict | None:
        stem = file_stem(file_name)
        candidates = []
        for f in worker.sd_list_dir("/cache"):
            match = BBL_NAME.match(f["name"])
            if match and match[2] == stem:
                candidates.append((f["modified"] or "", int(match[1]), f["name"]))
        if not candidates:
            return None
        _, plate, name = max(candidates)
        try:
            data = json.loads(worker.sd_read_small(f"/cache/{name}").decode("utf-8", "replace"))
        except (ValueError, Exception):
            return None
        data["plate"] = plate
        return data

    @staticmethod
    def _sd_path(bbl_path: str | None, file_name: str) -> str | None:
        if bbl_path and bbl_path.startswith("/sdcard/"):
            return bbl_path[len("/sdcard"):]
        return f"/{file_name}" if "/" not in file_name else None

    @staticmethod
    def _slot_for(filament: dict, mapping, use_ams: bool, ams_slots: list) -> str | None:
        if not use_ams:
            return spools_module.EXTERNAL_SLOT
        try:
            index = int(filament["id"]) - 1
        except (TypeError, ValueError):
            index = -1
        if isinstance(mapping, list) and 0 <= index < len(mapping):
            return spools_module.slot_label_for_tray(int(mapping[index]))
        # Pas de correspondance connue : même matériau et couleur la plus proche
        suggestion = threemf.suggest_trays([filament], ams_slots)[0]
        return suggestion["slot"]

    # ── Fin d'impression : coût, stock, vidéo ──
    def finish(self, worker, job_id: int):
        job = self.history.job(job_id)
        for _ in range(12):                      # attend que l'historique ait clôturé l'impression
            if job and job["status"] != "running":
                break
            time.sleep(END_WAIT_SECONDS)
            job = self.history.job(job_id)
        if not job or job["status"] == "running":
            return
        costs = compute_costs(job, cost_settings())
        self.history.update_job_fields(job_id, **costs)
        plan = job.get("plan") or {}
        ratio = 1.0 if job["status"] == "finished" else max(0, min(100, job.get("last_percent") or 0)) / 100
        for spool in self.spools.consume(worker.id, usage_by_slot(plan, ratio)):
            self.history.add_event(
                "filament", "warn", f"Bobine presque vide : {spool['name']} ({spool['slot']})",
                f"Il reste environ {round(spool['remaining_g'])} g de {spool['material']}. "
                "Prévois une bobine de remplacement (page Filament).", worker.id, job_id)
        if plan.get("timelapse"):
            for _ in range(6):
                time.sleep(TIMELAPSE_RETRY_SECONDS)
                if self.import_timelapse(worker, job_id):
                    break

    def import_timelapse(self, worker, job_id: int) -> bool:
        """Copie la vidéo timelapse de l'impression (si elle existe) dans data/media/job_<id>/."""
        job = self.history.job(job_id)
        if not job or job.get("timelapse"):
            return bool(job and job.get("timelapse"))
        start = datetime.fromisoformat(job["started_at"])
        end = datetime.fromisoformat(job["ended_at"]) if job.get("ended_at") else datetime.now()
        best = None
        for f in worker.sd_list_dir(TIMELAPSE_FOLDER):
            when = video_time(f["name"])
            if when and start - timedelta(minutes=TIMELAPSE_MATCH_MINUTES) <= when <= end:
                gap = abs((when - start).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, f["name"])
        if not best:
            return False
        name = self.save_video(worker, job_id, best[1])
        self.history.update_job_fields(job_id, timelapse=name)
        log.info("[%s] Timelapse %s rattaché à l'impression %s", worker.id, best[1], job_id)
        return True

    def save_video(self, worker, job_id: int, sd_name: str) -> str:
        folder = media_dir(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        raw = folder / sd_name
        worker.sd_download_path(f"{TIMELAPSE_FOLDER}/{sd_name}", raw)
        if raw.suffix.lower() == ".mp4":
            return raw.name
        # .avi (A1 / P1) : converti en .mp4 pour être lu dans le navigateur, si ffmpeg est installé
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return raw.name
        out = raw.with_suffix(".mp4")
        result = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(raw), "-c:v", "libx264",
                                 "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                                capture_output=True, timeout=1800)
        if result.returncode == 0 and out.exists():
            raw.unlink(missing_ok=True)
            return out.name
        return raw.name

    def sync_timelapses(self, worker) -> dict:
        """Bouton « Récupérer les timelapses » : rattache chaque vidéo de la carte SD à son impression."""
        found, attached = 0, 0
        for f in worker.sd_list_dir(TIMELAPSE_FOLDER):
            when = video_time(f["name"])
            if not when:
                continue
            found += 1
            window = TIMELAPSE_MATCH_MINUTES
            jobs = self.history.jobs_between(worker.id, (when - timedelta(minutes=window)).isoformat(),
                                             (when + timedelta(minutes=window)).isoformat())
            job = next((j for j in jobs if not j["timelapse"] and j["ended_at"]), None)
            if job:
                name = self.save_video(worker, job["id"], f["name"])
                self.history.update_job_fields(job["id"], timelapse=name)
                attached += 1
        return {"found": found, "attached": attached}
