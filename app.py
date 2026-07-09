"""
Dashboard web pour imprimante(s) Bambu Lab.

Usage :
    pip install -r requirements.txt
    python3 setup_admin.py      # une seule fois
    python3 app.py
Puis ouvre http://localhost:5000 (ou http://<IP-de-ce-PC>:5000 depuis un
autre appareil du reseau local).
"""
from pathlib import Path

from flask import (
    Flask, jsonify, render_template, request, Response,
    redirect, url_for, session
)

import bambu_client as bc
import config
import models
import printers_store as store
from auth import verify_credentials, login_required

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

ALLOWED_EXTENSIONS = {".gcode"}

# Démarre tous les threads pour les imprimantes déjà enregistrées
bc.manager.load_from_store(store.list_printers())


def _selected_printer_id():
    """Détermine l'imprimante actuellement sélectionnée (session ou 1ère dispo)."""
    printers = store.list_printers()
    if not printers:
        return None
    pid = session.get("selected_printer")
    if pid and any(p["id"] == pid for p in printers):
        return pid
    # par défaut, la première imprimante de la liste
    session["selected_printer"] = printers[0]["id"]
    return printers[0]["id"]


# ── Authentification ──────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_credentials(username, password):
            session["logged_in"] = True
            session["username"] = username
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        return render_template("login.html", error="Identifiants incorrects.")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages ─────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    printers = store.list_printers()
    if not printers:
        return redirect(url_for("printers_page"))
    selected = _selected_printer_id()
    return render_template(
        "index.html",
        username=session.get("username"),
        printers=printers,
        selected_id=selected,
    )


@app.route("/printers", methods=["GET"])
@login_required
def printers_page():
    return render_template(
        "printers.html",
        username=session.get("username"),
        printers=store.list_printers(),
        model_choices=models.model_choices(),
    )


@app.route("/printers/select", methods=["POST"])
@login_required
def printers_select():
    pid = request.form.get("printer_id")
    if store.get_printer(pid):
        session["selected_printer"] = pid
    return redirect(url_for("index"))


@app.route("/printers/add", methods=["POST"])
@login_required
def printers_add():
    name = request.form.get("name", "").strip()
    model = request.form.get("model", "OTHER")
    ip = request.form.get("ip", "").strip()
    access_code = request.form.get("access_code", "").strip()
    serial = request.form.get("serial", "").strip()
    chamber_override_raw = request.form.get("has_chamber_override", "auto")

    if not name or not ip or not access_code or not serial:
        return render_template(
            "printers.html", username=session.get("username"),
            printers=store.list_printers(), model_choices=models.model_choices(),
            error="Tous les champs (nom, IP, access code, numéro de série) sont requis.",
        )

    has_chamber_override = None
    if chamber_override_raw == "yes":
        has_chamber_override = True
    elif chamber_override_raw == "no":
        has_chamber_override = False

    new_printer = store.add_printer(name, model, ip, access_code, serial,
                                     has_chamber_override=has_chamber_override)
    bc.manager.add_and_start(new_printer)
    session["selected_printer"] = new_printer["id"]
    return redirect(url_for("printers_page"))


@app.route("/printers/delete/<printer_id>", methods=["POST"])
@login_required
def printers_delete(printer_id):
    bc.manager.remove(printer_id)
    store.delete_printer(printer_id)
    if session.get("selected_printer") == printer_id:
        session.pop("selected_printer", None)
    return redirect(url_for("printers_page"))


# ── API (toutes paramétrées par printer_id, via ?printer=... ou session) ──
def _resolve_worker():
    pid = request.args.get("printer") or _selected_printer_id()
    return pid, bc.manager.get(pid) if pid else None


@app.route("/api/status")
@login_required
def api_status():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify({"connected": False, "last_error": "Aucune imprimante sélectionnée", "data": {}})
    with worker.lock:
        connected = worker.connected
        last_error = worker.last_error
    return jsonify({
        "connected": connected,
        "last_error": last_error,
        "data": worker.snapshot(),
    })


@app.route("/api/camera.jpg")
@login_required
def api_camera():
    pid, worker = _resolve_worker()
    if not worker:
        return "Aucune imprimante sélectionnée", 503
    jpeg, ts = worker.get_camera()
    if jpeg is None:
        return "Pas encore d'image", 503
    return Response(jpeg, mimetype="image/jpeg")


@app.route("/api/history")
@login_required
def api_history():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify([])
    limit = request.args.get("limit", default=200, type=int)
    return jsonify(worker.get_history(limit=limit))


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify({"error": "Aucune imprimante sélectionnée"}), 400

    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier reçu"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Nom de fichier vide"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": f"Extension non supportée ({ext}). Seuls les fichiers .gcode sont acceptés."
        }), 400

    start_after_upload = request.form.get("start_print", default="false").lower() == "true"
    plate_number = request.form.get("plate_number", default="1")
    use_ams = request.form.get("use_ams", default="true").lower() == "true"

    dest = bc.UPLOAD_DIR / file.filename
    file.save(dest)

    try:
        remote_path, started = worker.upload_and_maybe_print(
            dest, start_after_upload, plate_number=plate_number, use_ams=use_ams
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if start_after_upload:
        message = "Fichier envoyé et impression lancée" if started else \
                   "Fichier envoyé sur la carte SD, mais le lancement a échoué"
    else:
        message = "Fichier envoyé sur la carte SD."

    return jsonify({
        "success": True,
        "remote_path": remote_path,
        "print_started": started,
        "message": message,
    })


# ── Contrôle ──────────────────────────────────────────────
@app.route("/api/control/light", methods=["POST"])
@login_required
def control_light():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify({"error": "Aucune imprimante sélectionnée"}), 400
    on = (request.get_json(silent=True, force=True) or {}).get("on", True)
    try:
        ok = worker.set_light(bool(on))
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/control/temperature", methods=["POST"])
@login_required
def control_temperature():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify({"error": "Aucune imprimante sélectionnée"}), 400
    part = (request.get_json(silent=True, force=True) or {}).get("part")
    value = (request.get_json(silent=True, force=True) or {}).get("value")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return jsonify({"error": "Valeur de température invalide"}), 400

    try:
        if part == "nozzle":
            if value < 0 or value > 320:
                return jsonify({"error": "Température buse hors limites (0-320°C)"}), 400
            ok = worker.set_nozzle_temperature(value)
        elif part == "bed":
            if value < 0 or value > 120:
                return jsonify({"error": "Température plateau hors limites (0-120°C)"}), 400
            ok = worker.set_bed_temperature(value)
        else:
            return jsonify({
                "error": "La température de chambre ne peut pas être réglée : "
                         "l'imprimante/la librairie n'expose aucune commande "
                         "fiable pour ça (lecture seule)."
            }), 400
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/control/speed", methods=["POST"])
@login_required
def control_speed():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify({"error": "Aucune imprimante sélectionnée"}), 400
    level = (request.get_json(silent=True, force=True) or {}).get("level")
    try:
        level = int(level)
        if level not in (1, 2, 3, 4):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Niveau de vitesse invalide (1 à 4 attendu)"}), 400
    try:
        ok = worker.set_speed_level(level)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/control/fan", methods=["POST"])
@login_required
def control_fan():
    pid, worker = _resolve_worker()
    if not worker:
        return jsonify({"error": "Aucune imprimante sélectionnée"}), 400
    fan = (request.get_json(silent=True, force=True) or {}).get("fan")
    percent = (request.get_json(silent=True, force=True) or {}).get("percent")
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return jsonify({"error": "Pourcentage invalide"}), 400
    if fan not in ("part", "aux", "chamber"):
        return jsonify({"error": "Ventilateur inconnu"}), 400
    try:
        ok = worker.set_fan(fan, percent)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
