"""
Routes de l'intelligence artificielle, de la file d'impression et des
informations de fichiers (.3mf). Regroupées ici pour ne pas alourdir app.py.
"""
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

from flask import Blueprint, Response, flash, jsonify, redirect, request, send_from_directory, url_for

import ai_client
import ai_features
import ai_monitor
import ai_tasks
import bambu_client as bc
import commands
import config
import print_queue
import printers_store as store
import sd_card
import threemf
import auth
from auth import login_required

log = logging.getLogger("ai_routes")
FILE_CACHE_DIR = config.DATA_DIR / "file_cache"
PHOTO_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.jpg$")


def create_blueprint(resolve_worker, monitor: ai_monitor.AiMonitor) -> Blueprint:
    bp = Blueprint("ai", __name__)

    def worker_or_error():
        _, worker = resolve_worker()
        if not worker:
            return None, (jsonify({"error": "Aucune imprimante sélectionnée"}), 400)
        return worker, None

    def ai_error(e: Exception, code: int = 502):
        return jsonify({"error": str(e)}), code

    def background(fn):
        """Lance une tâche IA longue et répond tout de suite avec son identifiant."""
        if not ai_features.get_config()["enabled"]:
            return ai_error(ai_client.AiError("L'intelligence artificielle est désactivée "
                                              "(Paramètres → Intelligence artificielle)."))
        return jsonify({"task": ai_tasks.start(fn)}), 202

    @bp.route("/api/ai/tasks/<task_id>")
    @login_required
    def ai_task(task_id):
        task = ai_tasks.get(task_id)
        if task is None:
            return jsonify({"status": "error", "error": "Tâche introuvable (serveur redémarré ?)."}), 404
        return jsonify(task)

    # ── Réglages IA (formulaire de la page Paramètres) ──
    @bp.route("/settings/ai", methods=["POST"])
    @login_required
    def settings_ai():
        form = request.form
        # part : "services" ou "automations" (assistant, une étape chacun) ; sinon les deux.
        part = form.get("part", "all")
        updates = {}
        if part in ("all", "services"):
            updates.update({
                "enabled": form.get("enabled") == "on",
                "provider": form.get("provider") if form.get("provider") in ai_client.PROVIDERS else "ollama",
                "base_url": form.get("base_url", "").strip(),
                "text_model": form.get("text_model", "").strip(),
                "vision_model": form.get("vision_model", "").strip(),
                "vision_same": form.get("vision_same") == "on",
                "vision_base_url": form.get("vision_base_url", "").strip(),
            })
            try:
                updates["timeout"] = max(10, min(600, int(form.get("timeout", ""))))
            except ValueError:
                pass
            if form.get("vision_provider") in ai_client.PROVIDERS:
                updates["vision_provider"] = form["vision_provider"]
            for field in ("api_key", "vision_api_key"):
                if form.get(field, "").strip():          # vide = on garde la clé enregistrée
                    updates[field] = form[field].strip()
                if form.get(f"clear_{field}") == "on":
                    updates[field] = ""
        if part in ("all", "automations"):
            for key in ("watch_enabled", "watch_log", "auto_pause", "plate_check", "finish_photo",
                        "finish_light_off", "filament_alert", "daily_summary"):
                updates[key] = form.get(key) == "on"
            for key, low, high in (("watch_interval_min", 1, 120), ("pause_confidence", 50, 100)):
                try:
                    updates[key] = max(low, min(high, int(form.get(key, ""))))
                except ValueError:
                    pass
            if re.fullmatch(r"\d{1,2}:\d{2}", form.get("daily_summary_time", "")):
                updates["daily_summary_time"] = form["daily_summary_time"]
        ai_features.save_config(updates)
        flash("Réglages de l'intelligence artificielle enregistrés." if part != "automations"
              else "Automatisations enregistrées.", "ok")
        if form.get("next"):
            return redirect(auth.safe_next_url(form["next"]))
        return redirect(url_for("settings_page") + "#ai")

    @bp.route("/api/ai/test", methods=["POST"])
    @login_required
    def ai_test():
        """Teste la connexion et liste les modèles (la clé enregistrée sert si le champ est vide)."""
        body = request.get_json(silent=True) or {}
        saved = ai_features.get_config()
        prefix = "vision_" if body.get("target") == "vision" else ""   # bloc « Texte » ou « Images »
        provider = body.get("provider") if body.get("provider") in ai_client.PROVIDERS else saved[f"{prefix}provider"]
        key = (body.get("api_key") or "").strip() or saved[f"{prefix}api_key"]
        try:
            models = ai_client.AiClient(provider, (body.get("base_url") or "").strip(), key, timeout=15).list_models()
        except ai_client.AiError as e:
            return ai_error(e)
        return jsonify({"models": models, "message": f"Connexion réussie : {len(models)} modèle(s) disponible(s)."})

    # ── Assistant ──
    @bp.route("/api/ai/chat", methods=["POST"])
    @login_required
    def ai_chat():
        messages = (request.get_json(silent=True) or {}).get("messages") or []
        if not messages:
            return jsonify({"error": "Message vide"}), 400
        pid, worker = resolve_worker()
        context = _assistant_context(pid, worker)      # lu maintenant, pendant la requête
        return background(lambda: ai_features.assistant_reply(messages, context))

    def _assistant_context(pid, worker) -> dict:
        """État utile à l'assistant, le plus court possible (chaque mot ralentit un petit modèle)."""
        history = bc.manager.history
        stats = history.stats(pid, 30)
        context = {
            "30_derniers_jours": {k: stats[k] for k in ("jobs", "finished", "failed", "success_rate")},
            "dernieres_impressions": [f"{j.get('file_name')} : {j.get('status')}" for j in history.jobs(pid, 3)],
            "file_attente": [item["name"] for item in (print_queue.items(pid) if pid else [])],
        }
        if worker:
            data = worker.snapshot()
            printer = {"nom": data.get("printer_name"), "connexion": worker.connection_info()["connection"],
                       "etat": data.get("gcode_state"), "fichier": data.get("file_name"),
                       "progression_pct": data.get("mc_percent"), "reste_min": data.get("remaining_time_min"),
                       "couche": f"{data.get('current_layer')}/{data.get('total_layers')}",
                       "buse": f"{data.get('nozzle_temp')}/{data.get('nozzle_target')}",
                       "plateau": f"{data.get('bed_temp')}/{data.get('bed_target')}",
                       "lumiere": data.get("light_state"), "vitesse_pct": data.get("print_speed"),
                       "erreur": data.get("print_error"),
                       "ams": [f"{s['label']} {s.get('type') or 'vide'} {s.get('remaining') if s.get('remaining') is not None else '?'}%"
                               + (" (en cours)" if s.get("active") else "") for s in data.get("ams") or []]}
            context["imprimante"] = {k: v for k, v in printer.items() if v not in (None, "", [], "None/None")}
        return context

    @bp.route("/api/ai/explain", methods=["POST"])
    @login_required
    def ai_explain():
        code = str((request.get_json(silent=True) or {}).get("code") or "").strip()
        if not re.fullmatch(r"[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}", code):
            return jsonify({"error": "Code d'erreur invalide"}), 400
        _, worker = resolve_worker()
        model = worker.model if worker else ""
        return background(lambda: {"explanation": ai_features.explain_error(code, model)})

    @bp.route("/api/ai/summary", methods=["POST"])
    @login_required
    def ai_summary():
        body = request.get_json(silent=True) or {}
        days = max(1, min(int(body.get("days") or 7), 365))
        pid = None if body.get("printer") == "all" else (body.get("printer") or resolve_worker()[0])
        history = bc.manager.history
        stats, jobs, errors = history.stats(pid, days), history.jobs(pid, 60), history.errors(pid, 30)
        return background(lambda: {"summary": ai_features.summarize_history(stats, jobs, errors, days)})

    @bp.route("/api/ai/plate_check", methods=["POST"])
    @login_required
    def ai_plate_check():
        worker, error = worker_or_error()
        if error:
            return error
        try:
            result = ai_monitor.check_plate_now(worker)
        except ai_client.AiError as e:
            return ai_error(e)
        if result is None:
            return jsonify({"error": "Vérification indisponible : IA (modèle vision) ou caméra non disponible."}), 409
        return jsonify(result)

    @bp.route("/api/ai/watch_now", methods=["POST"])
    @login_required
    def ai_watch_now():
        """Photo + analyse IA tout de suite (bouton « Contrôler maintenant » des alertes)."""
        worker, error = worker_or_error()
        if error:
            return error
        return background(lambda: monitor.watch_now(worker))

    # ── Notifications ──
    @bp.route("/api/ai/events")
    @login_required
    def ai_events():
        history = bc.manager.history
        names = {p["id"]: p["name"] for p in store.list_printers()}
        events = history.events(request.args.get("limit", default=40, type=int))
        for event in events:
            event["printer_name"] = names.get(event["printer_id"])
            event["photo_url"] = url_for("ai.ai_photo", name=event["photo"]) if event["photo"] else None
        return jsonify({"events": events, "unread": history.unread_events()})

    @bp.route("/api/ai/events/read", methods=["POST"])
    @login_required
    def ai_events_read():
        bc.manager.history.mark_events_read()
        return jsonify({"success": True})

    @bp.route("/api/ai/events/delete", methods=["POST"])
    @login_required
    def ai_events_delete():
        """{"id": 12} efface une alerte ; {"all": true} les efface toutes (photos comprises)."""
        body = request.get_json(silent=True) or {}
        if body.get("all") is True:
            event_id = None
        else:
            try:
                event_id = int(body.get("id"))
            except (TypeError, ValueError):
                return jsonify({"error": "Alerte non précisée"}), 400
        unused = bc.manager.history.delete_events(event_id)
        for name in unused:
            if PHOTO_NAME.match(name):
                (ai_monitor.PHOTOS_DIR / name).unlink(missing_ok=True)
        return jsonify({"success": True, "photos_deleted": len(unused)})

    @bp.route("/api/ai/photo/<name>")
    @login_required
    def ai_photo(name):
        if not PHOTO_NAME.match(name):
            return "Nom invalide", 400
        return send_from_directory(ai_monitor.PHOTOS_DIR, name, mimetype="image/jpeg")

    # ── File d'impression ──
    @bp.route("/api/queue")
    @login_required
    def queue_list():
        pid, _ = resolve_worker()
        return jsonify({"items": print_queue.items(pid) if pid else [], "proposal": monitor.status(pid)["proposal"]})

    @bp.route("/api/queue/<action>", methods=["POST"])
    @login_required
    def queue_action(action):
        pid, worker = resolve_worker()
        if not pid:
            return jsonify({"error": "Aucune imprimante sélectionnée"}), 400
        body = request.get_json(silent=True) or {}
        if action == "add":
            name = str(body.get("name") or "")
            if not sd_card.is_safe_name(name):
                return jsonify({"error": "Nom de fichier invalide."}), 400
            try:
                options = commands.parse_start_options(body)
                plate = int(body.get("plate") or 1)
            except (ValueError, TypeError) as e:
                return jsonify({"error": str(e)}), 400
            items = print_queue.add(pid, name, plate, options)
            return jsonify({"items": items, "message": f"{name} ajouté à la file ({len(items)} en attente)."})
        if action in ("remove", "up"):
            index = int(body.get("index", -1))
            monitor.forget_proposal(pid)
            items = (print_queue.remove if action == "remove" else print_queue.move_up)(pid, index)
            return jsonify({"items": items})
        if action == "recheck":
            monitor.forget_proposal(pid)
            return jsonify({"message": "Nouvelle vérification dans quelques secondes."})
        if action == "launch":
            if not worker:
                return jsonify({"error": "Imprimante introuvable"}), 400
            first = print_queue.pop_first(pid)
            if not first:
                return jsonify({"error": "La file est vide."}), 409
            monitor.forget_proposal(pid)
            options = {k: v for k, v in first.items() if k not in ("name", "plate")}
            result = worker.execute("start", {**options, "filename": first["name"], "plate": first["plate"]})
            if result.status not in ("confirmed", "unconfirmed"):
                print_queue.add(pid, first["name"], first["plate"], options, first=True)   # on le garde, en tête
            return jsonify(result.to_json()), result.http_code
        return jsonify({"error": "Action inconnue"}), 404

    # ── Informations d'un fichier .3mf ──
    def _cache_key(pid: str, name: str, size: str) -> str:
        return hashlib.sha1(f"v{threemf.INFO_VERSION}:{pid}:{name}:{size}".encode()).hexdigest()[:16]

    @bp.route("/api/files/info")
    @login_required
    def file_info():
        pid, worker = resolve_worker()
        name, size = request.args.get("name", ""), request.args.get("size", "")
        if not worker or not sd_card.is_safe_name(name):
            return jsonify({"error": "Fichier ou imprimante invalide."}), 400
        if not name.lower().endswith(".3mf"):
            return jsonify({"error": "Infos détaillées disponibles seulement pour les fichiers .3mf."}), 400
        key = _cache_key(pid, name, size)
        FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached = FILE_CACHE_DIR / f"{key}.json"
        if cached.exists():
            info = json.loads(cached.read_text(encoding="utf-8"))
        else:
            info = _read_3mf(worker, name, key)
            if isinstance(info, tuple):
                return info
            cached.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        ams = worker.snapshot().get("ams") or []
        for plate in info["plates"]:
            plate["suggestions"] = threemf.suggest_trays(plate["filaments"], ams)
            plate["thumbnail_url"] = (url_for("ai.file_thumbnail", key=key, plate=plate["index"])
                                      if plate["has_thumbnail"] else None)
        return jsonify({**info, "key": key})

    def _read_3mf(worker, name, key):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "file.3mf"
            try:
                worker.download_sd_file(name, local)
                info = threemf.parse(local)
                for plate in info["plates"]:
                    image = threemf.thumbnail(local, plate["index"]) if plate["has_thumbnail"] else None
                    if image:
                        (FILE_CACHE_DIR / f"{key}_p{plate['index']}.png").write_bytes(image)
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 409
            except sd_card.SdCardError as e:
                return jsonify({"error": str(e)}), 502
            except Exception as e:   # archive abîmée ou format inattendu
                return jsonify({"error": f"Fichier .3mf illisible : {e}"}), 422
        return info

    @bp.route("/api/files/thumbnail/<key>/<int:plate>")
    @login_required
    def file_thumbnail(key, plate):
        path = FILE_CACHE_DIR / f"{key}_p{plate}.png"
        if not re.fullmatch(r"[0-9a-f]{16}", key) or not path.exists():
            return "Vignette introuvable", 404
        return Response(path.read_bytes(), mimetype="image/png", headers={"Cache-Control": "max-age=86400"})

    @bp.route("/api/files/describe", methods=["POST"])
    @login_required
    def file_describe():
        body = request.get_json(silent=True) or {}
        key, plate = str(body.get("key") or ""), int(body.get("plate") or 1)
        path = FILE_CACHE_DIR / f"{key}_p{plate}.png"
        if not re.fullmatch(r"[0-9a-f]{16}", key) or not path.exists():
            return jsonify({"error": "Ouvre d'abord les infos du fichier."}), 400
        info_path = FILE_CACHE_DIR / f"{key}.json"
        info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
        plate_info = next((p for p in info.get("plates", []) if p["index"] == plate), {})
        image = path.read_bytes()
        return background(lambda: {"description": ai_features.describe_thumbnail(image, plate_info)})

    return bp
