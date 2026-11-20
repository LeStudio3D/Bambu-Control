"""
Routes du stock de filament, des coûts et des médias (vignettes, timelapses)
de chaque impression. Regroupées ici pour ne pas alourdir app.py.
"""
import re

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_from_directory, url_for

import ai_tasks
import bambu_client as bc
import job_extras
import printers_store as store
import settings_store
import spools as spools_module
import auth
from auth import login_required

MEDIA_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.(png|jpg|mp4|avi|mkv)$", re.I)


def create_blueprint(resolve_worker, extras: job_extras.JobExtras, spool_store: spools_module.SpoolStore) -> Blueprint:
    bp = Blueprint("extras", __name__)

    # ── Stock de filament ──
    @bp.route("/filament")
    @login_required
    def filament_page():
        return render_template("filament.html", materials=spools_module.MATERIALS)

    @bp.route("/api/spools")
    @login_required
    def spools_list():
        printers = [{"id": p["id"], "name": p["name"]} for p in store.list_printers()]
        slots = {}
        for p in printers:   # emplacements proposés : ceux de l'AMS vu par l'imprimante + support externe
            worker = bc.manager.get(p["id"])
            labels = [s["label"] for s in (worker.snapshot().get("ams") or [])] if worker else []
            slots[p["id"]] = labels + [spools_module.EXTERNAL_SLOT]
        return jsonify({"spools": spool_store.list(), "printers": printers, "slots": slots})

    @bp.route("/api/spools", methods=["POST"])
    @bp.route("/api/spools/<int:spool_id>", methods=["POST"])
    @login_required
    def spools_save(spool_id=None):
        try:
            data = spools_module.parse_spool(request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if data["printer_id"] and not store.get_printer(data["printer_id"]):
            return jsonify({"error": "Imprimante inconnue."}), 400
        if spool_id and not spool_store.get(spool_id):
            return jsonify({"error": "Bobine introuvable."}), 404
        spool_id = spool_store.save(data, spool_id)
        return jsonify({"success": True, "id": spool_id, "message": f"Bobine « {data['name']} » enregistrée."})

    @bp.route("/api/spools/<int:spool_id>/archive", methods=["POST"])
    @login_required
    def spools_archive(spool_id):
        spool = spool_store.get(spool_id)
        if not spool:
            return jsonify({"error": "Bobine introuvable."}), 404
        spool_store.archive(spool_id)
        return jsonify({"success": True, "message": f"Bobine « {spool['name']} » retirée du stock."})

    # ── Coûts ──
    @bp.route("/settings/costs", methods=["POST"])
    @login_required
    def settings_costs():
        form, costs = request.form, {}
        for key, low, high in (("electricity_price_kwh", 0, 5), ("printer_power_w", 10, 3000),
                               ("default_filament_price_kg", 0, 1000)):
            try:
                value = float(form.get(key, "").replace(",", "."))
            except ValueError:
                continue
            if low <= value <= high:
                costs[key] = value
        settings_store.save({"costs": {**job_extras.cost_settings(), **costs}})
        flash("Réglages des coûts enregistrés (pour les prochaines impressions).", "ok")
        if form.get("next"):
            return redirect(auth.safe_next_url(form["next"]))
        return redirect(url_for("settings_page") + "#costs")

    @bp.route("/api/history/costs")
    @login_required
    def history_costs():
        pid = request.args.get("printer") or resolve_worker()[0]
        months = request.args.get("months", default=12, type=int)
        rows = bc.manager.history.monthly_costs(None if pid == "all" else pid, max(1, min(months, 36)))
        return jsonify({"months": rows, "settings": job_extras.cost_settings()})

    # ── Fiche d'une impression et ses médias ──
    @bp.route("/api/jobs/<int:job_id>")
    @login_required
    def job_detail(job_id):
        job = bc.manager.history.job(job_id)
        if not job:
            return jsonify({"error": "Impression introuvable."}), 404
        job.pop("ams_start", None)
        for key in ("thumbnail", "timelapse"):
            job[f"{key}_url"] = (url_for("extras.job_media", job_id=job_id, name=job[key]) if job.get(key) else None)
        job["photo_url"] = url_for("ai.ai_photo", name=job["photo"]) if job.get("photo") else None
        return jsonify(job)

    @bp.route("/api/jobs/<int:job_id>/media/<name>")
    @login_required
    def job_media(job_id, name):
        if not MEDIA_NAME.match(name):
            return "Nom invalide", 400
        download = request.args.get("download") == "1"
        return send_from_directory(job_extras.media_dir(job_id), name, as_attachment=download)

    @bp.route("/api/timelapses/sync", methods=["POST"])
    @login_required
    def timelapses_sync():
        _, worker = resolve_worker()
        if not worker or not worker.connected:
            return jsonify({"error": "Imprimante hors ligne : la carte SD n'est pas accessible."}), 409
        return jsonify({"task": ai_tasks.start(lambda: extras.sync_timelapses(worker))}), 202

    return bp
