"""
Configuration du dashboard Bambu Lab.

Les imprimantes elles-mêmes (IP, access code, serial) ne sont plus ici :
elles se configurent depuis l'interface web (page "Imprimantes"), stockées
dans printers.json. Ce fichier ne contient que la config du serveur.
"""
import os
# ── Authentification dashboard ────────────────────────────
# Génère le hash avec : python3 -c "from werkzeug.security import generate_password_hash as g; print(g('ton_mot_de_passe'))"
ADMIN_USERNAME = os.environ.get("DASHBOARD_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get(
    "DASHBOARD_PASSWORD_HASH",
    "pbkdf2:sha256:600000$placeholder$replace_me_run_generate_hash_script"
)

# ── Flask ─────────────────────────────────────────────────
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-moi-en-production")

# ── Chemins ───────────────────────────────────────────────
DB_PATH = "bambu_history.db"
UPLOAD_TMP_DIR = "uploads_tmp"

# ── Intervalles de polling (secondes) ─────────────────────
STATUS_POLL_INTERVAL = 2       # rafraîchissement des données imprimante
CAMERA_POLL_INTERVAL = 0.5     # ~2 images/seconde pour le flux vidéo
HISTORY_SAVE_INTERVAL = 30     # fréquence d'enregistrement en base
