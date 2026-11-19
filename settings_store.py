"""
Réglages persistants de l'application dans data/settings.json :
compte admin (hash), SECRET_KEY, hôte/port du serveur, options kiosk.

Le fichier est rempli depuis l'interface web. Les variables d'environnement
DASHBOARD_USER, DASHBOARD_PASSWORD_HASH et FLASK_SECRET_KEY restent
prioritaires (pratique pour Docker ou systemd).
"""
import json
import os
import secrets
import shutil
import threading

import config

_lock = threading.Lock()

DEFAULTS = {
    "admin_username": "admin",
    "admin_password_hash": None,
    "secret_key": None,
    "host": config.DEFAULT_HOST,
    "port": config.DEFAULT_PORT,
}


def ensure_data_dir():
    """Crée data/ et y déplace les anciens fichiers posés à la racine du projet."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    legacy_files = {
        config.BASE_DIR / "printers.json": config.PRINTERS_PATH,
        config.BASE_DIR / "bambu_history.db": config.DB_PATH,
    }
    for old, new in legacy_files.items():
        if old.exists() and not new.exists():
            shutil.move(str(old), str(new))
            print(f"[setup] {old.name} déplacé dans {config.DATA_DIR}")


def load() -> dict:
    with _lock:
        data = dict(DEFAULTS)
        if config.SETTINGS_PATH.exists():
            with open(config.SETTINGS_PATH, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        return data


def save(updates: dict) -> dict:
    """Fusionne `updates` dans settings.json et renvoie le résultat."""
    ensure_data_dir()
    current = load()
    current.update(updates)
    with _lock:
        tmp = config.SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        os.replace(tmp, config.SETTINGS_PATH)  # écriture atomique
    return current


def admin_credentials() -> tuple[str, str | None]:
    settings = load()
    username = os.environ.get("DASHBOARD_USER") or settings["admin_username"]
    password_hash = os.environ.get("DASHBOARD_PASSWORD_HASH") or settings["admin_password_hash"]
    return username, password_hash


def secret_key() -> str:
    """Renvoie la SECRET_KEY ; en génère une et la sauvegarde si absente."""
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    key = load().get("secret_key")
    if not key:
        key = secrets.token_hex(32)
        save({"secret_key": key})
    return key
