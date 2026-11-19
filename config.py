"""
Réglages techniques de Bambu Control.

Les secrets (compte admin, SECRET_KEY) ne sont PLUS ici : ils sont dans
data/settings.json, rempli depuis l'interface (création du compte, Paramètres).
Les imprimantes sont dans data/printers.json (gérées depuis l'interface).
"""
import os
from pathlib import Path

# ── Chemins (absolus : l'appli marche quel que soit le dossier de lancement) ──
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BAMBU_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "bambu_history.db"
PRINTERS_PATH = DATA_DIR / "printers.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
UPLOAD_TMP_DIR = DATA_DIR / "uploads_tmp"

# ── Serveur web (surchargé par data/settings.json) ────────
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000

# ── Upload ────────────────────────────────────────────────
MAX_UPLOAD_MB = 512
ALLOWED_UPLOAD_SUFFIXES = (".gcode.3mf", ".3mf", ".gcode")

# ── Intervalles (secondes) ────────────────────────────────
STATUS_POLL_INTERVAL = 1        # lecture de l'état MQTT en mémoire
HISTORY_SAVE_INTERVAL = 30      # fréquence d'enregistrement en base
FIRST_REPORT_TIMEOUT_SECONDS = 20  # à la connexion : une imprimante répond en 2-3 s
OFFLINE_AFTER_SECONDS = 45      # connexion établie puis plus aucun message → hors ligne
RECONNECT_DELAY_SECONDS = 10

# ── Sécurité login ────────────────────────────────────────
LOGIN_MAX_ATTEMPTS = 5          # tentatives ratées autorisées…
LOGIN_LOCK_SECONDS = 300        # …par IP sur cette durée
SESSION_DAYS = 30               # durée de la session (pratique en mode kiosk)
