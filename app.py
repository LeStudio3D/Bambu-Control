"""
Bambu Control — serveur web pour imprimante(s) Bambu Lab.

Usage :
    pip install -r requirements.txt
    python3 app.py
Puis ouvre http://localhost:5000 (ou http://<IP-de-cette-machine>:5000) :
au premier lancement, la page propose de créer le compte administrateur.

Secours (mot de passe oublié) :
    python3 app.py --reset-admin
"""
import ipaddress
import logging
import re
import shutil
import tempfile
from pathlib import Path
import sys
import threading
import time
from datetime import timedelta

from flask import (
    Flask, flash, get_flashed_messages, jsonify, render_template, request, Response,
    redirect, send_file, url_for, session
)
from werkzeug.utils import secure_filename

import ai_client
import ai_features
import ai_monitor
import ai_routes
import appliance
import auth
import backup
import bambu_client as bc
import commands
import config
import extras_routes
import job_extras
import models
import printer_check
import print_queue
import printers_store as store
import sd_card
import settings_store
import spools
from auth import login_required

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

settings_store.ensure_data_dir()

app = Flask(__name__)
app.secret_key = settings_store.secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=config.MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_DAYS),
    START_WORKERS=True,  # mis à False par les tests
)

monitor = ai_monitor.AiMonitor(bc.manager)   # surveillance IA + automatisations
spool_store = spools.SpoolStore(lambda: bc.manager.history)   # stock de filament
extras = job_extras.JobExtras(bc.manager, spool_store)        # coûts, stock, timelapses
_workers_started = False
_workers_lock = threading.Lock()


def start_printer_workers():
    """Démarre une seule fois les threads des imprimantes enregistrées."""
    global _workers_started
    with _workers_lock:
        if _workers_started or not app.config["START_WORKERS"]:
            return
        _workers_started = True
    bc.manager.load_from_store(store.list_printers())
    monitor.start()
    extras.start()


@app.before_request
def before_request():
    start_printer_workers()
    return auth.check_csrf()


# Numéro de version des fichiers CSS/JS (date de la dernière modification) : après une mise à jour,
# les navigateurs (dont le kiosk) rechargent les fichiers au lieu de garder l'ancienne version.
ASSET_VERSION = str(int(max(p.stat().st_mtime for p in (config.BASE_DIR / "static").rglob("*") if p.is_file())))


@app.context_processor
def inject_globals():
    """Variables disponibles dans tous les templates (barre d'état, navigation)."""
    data = {"csrf_token": auth.csrf_token, "asset_version": ASSET_VERSION,
            "theme": settings_store.load().get("theme", "dark")}
    if session.get("logged_in"):
        printers = [store.public_view(p) for p in store.list_printers()]
        selected = _selected_printer_id() if printers else None
        data.update(
            username=session.get("username"),
            nav_printers=printers,
            selected_id=selected,
            selected_printer=next((p for p in printers if p["id"] == selected), None),
            model_labels={k: v["label"] for k, v in models.PRINTER_MODELS.items()},
        )
    return data


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": f"Fichier trop gros (maximum {config.MAX_UPLOAD_MB} Mo)."}), 413


def _selected_printer_id():
    """Imprimante sélectionnée (session) ou, à défaut, la première de la liste."""
    printers = store.list_printers()
    if not printers:
        return None
    pid = session.get("selected_printer")
    if pid and any(p["id"] == pid for p in printers):
        return pid
    session["selected_printer"] = printers[0]["id"]
    return printers[0]["id"]


def _printers_page(error=None, status=200):
    return render_template(
        "settings.html",
        printers=[store.public_view(p) for p in store.list_printers()],
        model_choices=models.model_choices(),
        error=error,
        messages=get_flashed_messages(with_categories=True),
        port=settings_store.load()["port"],
        startup=appliance.status(),
        ai=ai_features.public_config(),
        ai_providers=ai_client.PROVIDERS,
        costs=job_extras.cost_settings(),
    ), status


def _back(anchor: str = ""):
    """Après un enregistrement : page demandée par le formulaire (assistant) ou Paramètres."""
    nxt = request.form.get("next")
    if nxt:
        return redirect(auth.safe_next_url(nxt))
    return redirect(url_for("settings_page") + anchor)


def _printer_form_error(message: str, status: int):
    """Erreur du formulaire d'imprimante : retour à l'assistant (message affiché) ou page Paramètres."""
    if request.form.get("next"):
        flash(message, "error")
        return _back()
    return _printers_page(message, status)


def _log_in(username: str):
    session.clear()  # nouvelle session : évite la fixation de session
    session.permanent = True
    session["logged_in"] = True
    session["username"] = username


HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")


def is_valid_host(value: str) -> bool:
    """Adresse IP (192.168.1.20) ou nom réseau (a1.local)."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return len(value) <= 253 and bool(HOSTNAME.match(value))


def _chamber_override_from_form(raw: str):
    return {"yes": True, "no": False}.get(raw)  # "auto" → None


# ── Premier lancement : création du compte ────────────────
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if auth.is_admin_configured():
        return redirect(url_for("login"))
    local = auth.is_local_request()
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not local and not auth.setup_code_ok(request.form.get("setup_code", "")):
            error = "Code de configuration incorrect (voir ci-dessous comment le retrouver)."
        elif not username:
            error = "Choisis un identifiant."
        else:
            error = auth.password_problem(password, request.form.get("password_confirm", ""))
        if error is None:
            auth.save_admin(username, password)
            log.info("Compte administrateur « %s » créé depuis l'interface", username)
            _log_in(username)
            return redirect(url_for("welcome"))
    return render_template("setup.html", local=local, error=error,
                           code_file=auth.SETUP_CODE_FILE), (400 if error else 200)


# ── Authentification ──────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    setup_needed = not auth.is_admin_configured()
    if setup_needed:
        return redirect(url_for("setup"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if auth.is_login_locked(ip):
            return render_template("login.html", setup_needed=False,
                                   error="Trop de tentatives. Réessaie dans quelques minutes."), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if auth.verify_credentials(username, password):
            auth.reset_login_failures(ip)
            _log_in(username)
            return redirect(auth.safe_next_url(request.args.get("next")))
        auth.record_login_failure(ip)
        time.sleep(0.5)  # ralentit les essais en rafale
        return render_template("login.html", setup_needed=False, error="Identifiants incorrects."), 401
    return render_template("login.html", setup_needed=setup_needed, error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages ─────────────────────────────────────────────────
def _console_page(template: str):
    """Pages qui ont besoin d'une imprimante : sinon direction Paramètres pour en ajouter une."""
    if not store.list_printers():
        return redirect(url_for("welcome"))
    return render_template(template)


@app.route("/")
@login_required
def index():
    return _console_page("home.html")


@app.route("/printer")
@login_required
def printer_page():
    return _console_page("printer.html")


@app.route("/prints")
@login_required
def prints_page():
    return _console_page("prints.html")


@app.route("/history")
@login_required
def history_page():
    return _console_page("history.html")


@app.route("/settings")
@login_required
def settings_page():
    return _printers_page()


# ── Assistant de première configuration ───────────────────
WELCOME_STEPS = (
    ("printers", "Imprimantes"),
    ("server", "Serveur"),
    ("costs", "Coûts"),
    ("ai", "Intelligence artificielle"),
    ("automations", "Automatisations"),
    ("filament", "Stock de filament"),
    ("done", "Terminé"),
)


@app.route("/welcome")
@login_required
def welcome():
    """Assistant pas à pas, ouvert après la création du compte (et relançable depuis Paramètres)."""
    keys = [key for key, _ in WELCOME_STEPS]
    step = request.args.get("step", keys[0])
    if step not in keys:
        step = keys[0]
    index = keys.index(step)
    return render_template(
        "welcome.html",
        steps=WELCOME_STEPS,
        step=step,
        step_index=index,
        next_step=url_for("welcome", step=keys[index + 1]) if index + 1 < len(keys) else url_for("index"),
        prev_step=url_for("welcome", step=keys[index - 1]) if index > 0 else None,
        here=url_for("welcome", step=step),
        printers=[store.public_view(p) for p in store.list_printers()],
        model_choices=models.model_choices(),
        messages=get_flashed_messages(with_categories=True),
        port=settings_store.load()["port"],
        startup=appliance.status(),
        ai=ai_features.public_config(),
        ai_providers=ai_client.PROVIDERS,
        costs=job_extras.cost_settings(),
        materials=spools.MATERIALS,
    )


@app.route("/settings/account", methods=["POST"])
@login_required
def settings_account():
    """Changer l'identifiant et/ou le mot de passe (le mot de passe actuel est demandé)."""
    current_user, _ = settings_store.admin_credentials()
    if not auth.verify_credentials(current_user, request.form.get("current_password", "")):
        flash("Mot de passe actuel incorrect : rien n'a été modifié.", "error")
        return redirect(url_for("settings_page"))
    username = request.form.get("username", "").strip() or current_user
    new_password = request.form.get("new_password", "")
    if new_password:
        problem = auth.password_problem(new_password, request.form.get("new_password_confirm", ""))
        if problem:
            flash(problem, "error")
            return redirect(url_for("settings_page"))
    auth.save_admin(username, new_password or None)
    session["username"] = username
    flash("Compte mis à jour." if new_password else "Identifiant mis à jour.", "ok")
    return redirect(url_for("settings_page"))


# ── Sauvegarde complète : export (Paramètres) et import (assistant) ──
IMPORT_MAX_BYTES = 8 * 1024 ** 3   # l'historique avec les timelapses peut être gros


@app.route("/settings/export", methods=["POST"])
@login_required
def settings_export():
    password = request.form.get("backup_password", "")
    if password != request.form.get("backup_password_confirm", ""):
        flash("Les deux mots de passe de la sauvegarde ne correspondent pas.", "error")
        return redirect(url_for("settings_page") + "#backup")
    try:
        path = backup.build_export(bc.manager.history, password)
    except backup.BackupError as e:
        flash(str(e), "error")
        return redirect(url_for("settings_page") + "#backup")
    log.info("Sauvegarde exportée (%s Mo)", round(path.stat().st_size / 1024 ** 2, 1))
    response = send_file(path, as_attachment=True, download_name=path.name,
                         mimetype="application/octet-stream", max_age=0)
    response.call_on_close(lambda: shutil.rmtree(path.parent, ignore_errors=True))
    return response


@app.route("/settings/import", methods=["POST"])
@login_required
def settings_import():
    """Remplace réglages, imprimantes, historique, bobines et médias par ceux de la sauvegarde."""
    request.max_content_length = IMPORT_MAX_BYTES
    back = url_for("welcome", step="printers")
    upload = request.files.get("backup_file")
    if not upload or not upload.filename:
        flash("Choisis le fichier de sauvegarde (.bcbackup).", "error")
        return redirect(back)
    if store.list_printers() and request.form.get("confirm_replace") != "on":
        flash("Coche la case pour confirmer le remplacement des réglages actuels.", "error")
        return redirect(back)
    work = Path(tempfile.mkdtemp(prefix="bc-import-", dir=config.DATA_DIR))
    try:
        encrypted = work / "upload.bcbackup"
        upload.save(encrypted)
        data = backup.read_backup(encrypted, request.form.get("backup_password", ""), work)
        encrypted.unlink()
        old_port = settings_store.load()["port"]

        for printer_id in bc.manager.list_ids():   # plus personne n'écrit pendant le remplacement
            bc.manager.remove(printer_id)
        settings_store.save(data["settings"])
        store.replace_all(data["printers"])
        if data["queue"] is not None:
            print_queue.replace_all(data["queue"])
        if data["db_path"]:
            backup.restore_database(bc.manager.history, data["db_path"])
            bc.manager.history.ensure_schema()      # sauvegarde d'une version plus ancienne
            spools.SpoolStore.ensure_table(bc.manager.history)
        backup.restore_media(data["media_dir"])
        bc.manager.load_from_store(store.list_printers())
        session.pop("selected_printer", None)

        new_port = settings_store.load()["port"]
        if appliance.status()["kiosk_enabled"]:
            appliance.enable_kiosk(int(new_port))
        names = ", ".join(p["name"] for p in data["printers"]) or "aucune"
        flash(f"Sauvegarde importée : imprimantes ({names}), réglages, historique, bobines et "
              f"{data['media_count']} photo(s) / vidéo(s). Vérifie chaque étape puis clique sur Suivant.", "ok")
        if new_port != old_port:
            flash(f"La sauvegarde utilise le port {new_port} : il s'appliquera au prochain démarrage "
                  f"de Bambu Control (en attendant, l'adresse reste :{old_port}).", "ok")
        log.info("Sauvegarde importée (%d imprimante(s))", len(data["printers"]))
    except backup.BackupError as e:
        flash(str(e), "error")
    except Exception:
        log.exception("Import de sauvegarde impossible")
        flash("Import impossible : erreur inattendue (détails dans le journal de Bambu Control).", "error")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return redirect(back)


@app.route("/settings/server", methods=["POST"])
@login_required
def settings_server():
    raw = request.form.get("port", "").strip()
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        flash("Port invalide : choisis un nombre entre 1024 et 65535.", "error")
        return _back()
    settings_store.save({"port": int(raw)})
    if appliance.status()["kiosk_enabled"]:
        appliance.enable_kiosk(int(raw))     # le plein écran doit ouvrir la nouvelle adresse
    flash(f"Port {raw} enregistré : il sera utilisé au prochain démarrage de Bambu Control.", "ok")
    return _back()


@app.route("/settings/theme", methods=["POST"])
@login_required
def settings_theme():
    theme = request.form.get("theme")
    if theme not in ("dark", "light"):
        flash("Thème inconnu.", "error")
        return _back("#appearance")
    settings_store.save({"theme": theme})
    flash("Thème clair activé." if theme == "light" else "Thème sombre activé.", "ok")
    return _back("#appearance")


@app.route("/settings/startup", methods=["POST"])
@login_required
def settings_startup():
    """Démarrage automatique et plein écran (Linux) : chaque case est appliquée telle quelle."""
    current = appliance.status()
    port = settings_store.load()["port"]
    wanted_autostart = request.form.get("autostart") == "on"
    wanted_kiosk = request.form.get("kiosk") == "on"
    results = []
    if current["can_autostart"] and wanted_autostart != current["autostart_enabled"]:
        results.append(appliance.enable_autostart() if wanted_autostart else appliance.disable_autostart())
    if current["can_kiosk"] and wanted_kiosk != current["kiosk_enabled"]:
        results.append(appliance.enable_kiosk(port) if wanted_kiosk else appliance.disable_kiosk())
    if not results:
        flash("Aucun changement.", "ok")
    for ok, message in results:
        flash(message, "ok" if ok else "error")
    return _back()


@app.route("/printers", methods=["GET"])
@login_required
def printers_page():
    return redirect(url_for("settings_page"))  # ancienne adresse


@app.route("/printers/select", methods=["POST"])
@login_required
def printers_select():
    pid = request.form.get("printer_id")
    if pid and store.get_printer(pid):
        session["selected_printer"] = pid
    return redirect(auth.safe_next_url(request.form.get("next")))


@app.route("/printers/add", methods=["POST"])
@login_required
def printers_add():
    name = request.form.get("name", "").strip()
    model = request.form.get("model", "OTHER")
    ip = request.form.get("ip", "").strip()
    access_code = request.form.get("access_code", "").strip()
    serial = request.form.get("serial", "").strip()

    if not name or not ip or not access_code or not serial:
        return _printer_form_error("Tous les champs (nom, IP, access code, numéro de série) sont requis.", 400)
    if not is_valid_host(ip):
        return _printer_form_error(f"Adresse IP invalide : « {ip} ».", 400)
    if model not in models.PRINTER_MODELS:
        model = "OTHER"

    new_printer = store.add_printer(
        name, model, ip, access_code, serial,
        has_chamber_override=_chamber_override_from_form(request.form.get("has_chamber_override", "auto")),
    )
    bc.manager.add_and_start(new_printer)
    session["selected_printer"] = new_printer["id"]
    if request.form.get("next"):
        flash(f"Imprimante « {name} » ajoutée.", "ok")
    return _back()


@app.route("/printers/edit/<printer_id>", methods=["POST"])
@login_required
def printers_edit(printer_id):
    if not store.get_printer(printer_id):
        return _printers_page("Imprimante introuvable.", 404)
    fields = {}
    for key in ("name", "ip", "serial"):   # champ vide = on garde l'ancienne valeur
        value = request.form.get(key, "").strip()
        if value:
            fields[key] = value
    if "ip" in fields and not is_valid_host(fields["ip"]):
        return _printers_page(f"Adresse IP invalide : « {fields['ip']} ».", 400)
    access_code = request.form.get("access_code", "").strip()
    if access_code:  # champ vide = on garde l'ancien code
        fields["access_code"] = access_code
    model = request.form.get("model")
    if model in models.PRINTER_MODELS:
        fields["model"] = model
    if "has_chamber_override" in request.form:
        fields["has_chamber_override"] = _chamber_override_from_form(request.form["has_chamber_override"])

    updated = store.update_printer(printer_id, **fields)
    bc.manager.restart(updated)
    return redirect(url_for("settings_page"))


@app.route("/api/printers/test", methods=["POST"])
@login_required
def api_printers_test():
    """
    Teste une connexion. Pour une imprimante existante (printer_id), les
    champs laissés vides reprennent les valeurs enregistrées : l'access code
    et le numéro de série ne repassent jamais par le navigateur.
    """
    body = request.get_json(silent=True) or {}
    saved = store.get_printer(body.get("printer_id") or "") or {}
    ip = (body.get("ip") or saved.get("ip") or "").strip()
    access_code = (body.get("access_code") or saved.get("access_code") or "").strip()
    serial = (body.get("serial") or saved.get("serial") or "").strip()
    model = body.get("model") or saved.get("model") or "OTHER"
    if not ip or not access_code or not serial:
        return jsonify({"error": "IP, access code et numéro de série sont nécessaires pour le test."}), 400
    if not is_valid_host(ip):
        return jsonify({"error": f"Adresse IP invalide : « {ip} »."}), 400
    return jsonify(printer_check.check_printer(ip, access_code, serial, model))


@app.route("/printers/delete/<printer_id>", methods=["POST"])
@login_required
def printers_delete(printer_id):
    bc.manager.remove(printer_id)
    store.delete_printer(printer_id)
    if session.get("selected_printer") == printer_id:
        session.pop("selected_printer", None)
    return _back()


# ── API (paramétrée par ?printer=<id>, sinon l'imprimante sélectionnée) ──
def _resolve_worker():
    pid = request.args.get("printer") or _selected_printer_id()
    return pid, bc.manager.get(pid) if pid else None


def _no_printer():
    return jsonify({"error": "Aucune imprimante sélectionnée"}), 400


@app.route("/api/printers")
@login_required
def api_printers():
    result = []
    for p in store.list_printers():
        worker = bc.manager.get(p["id"])
        info = worker.connection_info() if worker else {"connection": bc.OFFLINE, "connected": False,
                                                        "last_error": "Non démarrée"}
        result.append({**store.public_view(p), **info})
    return jsonify(result)


@app.route("/api/status")
@login_required
def api_status():
    _, worker = _resolve_worker()
    if not worker:
        return jsonify({"connected": False, "connection": bc.OFFLINE,
                        "last_error": "Aucune imprimante sélectionnée", "data": {}})
    pid, _ = _resolve_worker()
    data = worker.snapshot()
    data["ai"] = monitor.status(pid)
    # Bobines du stock posées sur cette imprimante : poids restant affiché sur les emplacements
    data["spools"] = {slot: {"id": s["id"], "name": s["name"], "remaining_g": s["remaining_g"], "low": s["low"]}
                      for slot, s in spool_store.by_slot(pid).items()}
    return jsonify({**worker.connection_info(), "data": data})


@app.route("/api/camera.jpg")
@login_required
def api_camera():
    """Dernière image de la caméra. 503 + message clair si indisponible."""
    _, worker = _resolve_worker()
    if not worker:
        return jsonify({"state": "unavailable", "message": "Aucune imprimante sélectionnée"}), 503
    jpeg, frame_id = worker.get_camera()
    if jpeg is None:
        return jsonify(worker.camera_status()), 503
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Frame-Id": str(frame_id)})


def _history_printer_filter():
    """?printer=all → toutes les imprimantes ; sinon l'imprimante demandée/sélectionnée."""
    pid = request.args.get("printer")
    if pid == "all":
        return None
    return pid or _selected_printer_id()


@app.route("/api/history/jobs")
@login_required
def api_history_jobs():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(bc.manager.history.jobs(_history_printer_filter(), limit))


@app.route("/api/history/clear", methods=["POST"])
@login_required
def api_history_clear():
    """{"printer": id | "all"} : efface impressions, courbes et erreurs (l'impression en cours est gardée)."""
    pid = str((request.get_json(silent=True) or {}).get("printer") or "")
    if pid != "all" and not store.get_printer(pid):
        return jsonify({"error": "Imprimante inconnue."}), 400
    counts, unused_photos = bc.manager.history.clear(None if pid == "all" else pid)
    for name in unused_photos:
        if re.fullmatch(r"[A-Za-z0-9_.-]+\.jpg", name):
            (ai_monitor.PHOTOS_DIR / name).unlink(missing_ok=True)
    job_extras.remove_media(counts.pop("job_ids", []))      # vignettes et timelapses
    who = "de toutes les imprimantes" if pid == "all" else f"de « {store.get_printer(pid)['name']} »"
    log.info("Historique effacé %s : %s", who, counts)
    return jsonify({"success": True, "counts": counts,
                    "message": f"Historique {who} effacé : {counts['jobs']} impression(s), "
                               f"{counts['errors']} erreur(s)."})


@app.route("/api/history/errors")
@login_required
def api_history_errors():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(bc.manager.history.errors(_history_printer_filter(), limit))


@app.route("/api/history/stats")
@login_required
def api_history_stats():
    days = request.args.get("days", default=30, type=int)
    return jsonify(bc.manager.history.stats(_history_printer_filter(), max(1, min(days, 365))))


@app.route("/api/history/samples")
@login_required
def api_history_samples():
    pid = request.args.get("printer") or _selected_printer_id()
    if not pid or pid == "all":
        return jsonify({"error": "Choisis une imprimante pour afficher les courbes."}), 400
    job_id = request.args.get("job", type=int)
    hours = request.args.get("hours", default=24, type=float)
    return jsonify(bc.manager.history.samples(pid, job_id=job_id, hours=max(1, min(hours, 24 * 30))))


@app.route("/api/history")
@login_required
def api_history():
    _, worker = _resolve_worker()
    if not worker:
        return jsonify([])
    limit = request.args.get("limit", default=200, type=int)
    return jsonify(worker.get_history(limit=limit))


def _allowed_upload(filename: str) -> bool:
    return filename.lower().endswith(config.ALLOWED_UPLOAD_SUFFIXES)


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    _, worker = _resolve_worker()
    if not worker:
        return _no_printer()

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Aucun fichier reçu"}), 400

    filename = secure_filename(file.filename)  # supprime "../", "/" etc.
    if not filename or not _allowed_upload(filename):
        return jsonify({"error": "Format non supporté. Fichiers acceptés : .gcode.3mf, .3mf, .gcode "
                                 "(nom en caractères simples)."}), 400

    start_after_upload = request.form.get("start_print", "false").lower() == "true"
    dest = bc.UPLOAD_DIR / filename
    file.save(dest)
    try:
        remote_path = worker.upload(dest)
    except RuntimeError as e:          # imprimante hors ligne
        return jsonify({"error": f"Envoi impossible : {e}."}), 409
    except sd_card.SdCardError as e:
        log.warning("Upload échoué : %s", e)
        return jsonify({"error": str(e)}), 502
    finally:
        dest.unlink(missing_ok=True)

    response = {"success": True, "remote_path": remote_path, "filename": filename,
                "message": f"{filename} envoyé sur la carte SD."}
    if start_after_upload:
        params = {"filename": filename,
                  "plate": request.form.get("plate_number", default=1, type=int) or 1,
                  "use_ams": request.form.get("use_ams", "true").lower() == "true"}
        refusal = _plate_check_refusal(worker)
        if refusal:
            response["print"] = {"status": commands.REJECTED, "message": refusal["message"], "success": False}
            response["message"] += " Lancement annulé : " + refusal["message"] + \
                " Lance-le depuis la liste « Sur la carte SD » une fois le plateau vidé."
            return jsonify(response)
        result = worker.execute("start", params)
        response["print"] = result.to_json()
        response["message"] += " " + result.message
    return jsonify(response)


def _plate_check_refusal(worker) -> dict | None:
    """
    Vérification du plateau par l'IA avant un lancement (si activée).
    Renvoie un refus {message, plate_check} si l'IA voit quelque chose sur le
    plateau ; None sinon. Une IA indisponible ne bloque jamais l'impression.
    """
    try:
        check = ai_monitor.check_plate_now(worker)
    except ai_client.AiError as e:
        log.warning("Vérification du plateau impossible : %s", e)
        return None
    if check and not check["clear"]:
        return {"message": f"l'IA pense que le plateau n'est pas vide ({check['description']})",
                "plate_check": check}
    return None


# ── Fichiers de la carte SD ───────────────────────────────
@app.route("/api/files")
@login_required
def api_files():
    _, worker = _resolve_worker()
    if not worker:
        return _no_printer()
    try:
        return jsonify({"files": worker.list_sd_files()})
    except RuntimeError as e:
        return jsonify({"error": f"{e} : la carte SD n'est pas accessible."}), 409
    except sd_card.SdCardError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/files/delete", methods=["POST"])
@login_required
def api_files_delete():
    _, worker = _resolve_worker()
    if not worker:
        return _no_printer()
    name = str((request.get_json(silent=True) or {}).get("name") or "")
    if not sd_card.is_safe_name(name):
        return jsonify({"error": "Nom de fichier invalide."}), 400
    try:
        worker.delete_sd_file(name)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    except sd_card.SdCardError as e:
        return jsonify({"error": str(e)}), 502
    log.info("Fichier supprimé de la carte SD : %s", name)
    return jsonify({"success": True, "message": f"{name} supprimé de la carte SD."})


# ── Commandes ─────────────────────────────────────────────
@app.route("/api/command/<name>", methods=["POST"])
@login_required
def api_command(name):
    """
    Exécute une commande (pause, resume, stop, light, nozzle_temp, bed_temp,
    speed, fan, start) et attend la confirmation de l'imprimante.
    Réponse : {"status": confirmed|unconfirmed|rejected|error, "message": ...}
    """
    if name not in commands.COMMANDS:
        return jsonify({"status": commands.REJECTED, "message": "Commande inconnue"}), 404
    _, worker = _resolve_worker()
    if not worker:
        return _no_printer()
    try:
        params = commands.COMMANDS[name].parse(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({"status": commands.REJECTED, "message": str(e)}), 400
    if name == "fan" and params["fan"] not in worker.fans:
        return jsonify({"status": commands.REJECTED,
                        "message": "Ce modèle n'a pas ce ventilateur."}), 400
    if name == "start" and not (request.get_json(silent=True) or {}).get("force"):
        refusal = _plate_check_refusal(worker)
        if refusal:
            return jsonify({"status": commands.REJECTED, "success": False, "plate_check": refusal["plate_check"],
                            "message": f"Lancement suspendu : {refusal['message']}."}), 409
    result = worker.execute(name, params)
    return jsonify(result.to_json()), result.http_code


app.register_blueprint(ai_routes.create_blueprint(_resolve_worker, monitor))
app.register_blueprint(extras_routes.create_blueprint(_resolve_worker, extras, spool_store))


def main():
    if "--reset-admin" in sys.argv:
        auth.reset_admin()
        print("Compte administrateur supprimé. Relance Bambu Control puis ouvre la page web "
              "pour en créer un nouveau (tes imprimantes et ton historique sont conservés).")
        return
    if "--install" in sys.argv or "--uninstall" in sys.argv:
        # Utilisé par deploy/install.sh et deploy/uninstall.sh (Linux).
        # --install active les deux, sauf --no-autostart / --no-kiosk ; --uninstall enlève les deux.
        settings_store.ensure_data_dir()
        port = int(settings_store.load()["port"])
        removing = "--uninstall" in sys.argv
        want_autostart = not removing and "--no-autostart" not in sys.argv
        want_kiosk = not removing and "--no-kiosk" not in sys.argv
        for label, (ok, message) in (
                ("Démarrage automatique",
                 appliance.enable_autostart() if want_autostart else appliance.disable_autostart()),
                ("Plein écran (kiosk)",
                 appliance.enable_kiosk(port) if want_kiosk else appliance.disable_kiosk())):
            print(f"[{'ok' if ok else '!!'}] {label} : {message}")
        return
    settings = settings_store.load()
    host, port = settings["host"], int(settings["port"])
    if not auth.is_admin_configured():
        log.warning("Premier lancement : ouvre http://localhost:%s pour créer le compte administrateur.", port)
        log.warning("Depuis un autre appareil du réseau, le code demandé est : %s", auth.SETUP_CODE)
        auth.publish_setup_code()
    else:
        auth.clear_setup_code()
    start_printer_workers()
    try:
        from waitress import serve
        log.info("Bambu Control démarré sur http://%s:%s (waitress)", host, port)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        log.info("waitress absent : serveur de développement Flask utilisé")
        app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
