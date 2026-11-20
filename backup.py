"""
Sauvegarde complète de Bambu Control dans un seul fichier chiffré (.bcbackup).

Contenu : réglages (serveur, coûts, IA, automatisations), imprimantes avec
leurs access codes et numéros de série, base de l'historique (impressions,
alertes, stock de filament), photos, vignettes, timelapses et file d'attente.
Le compte administrateur et la SECRET_KEY ne sont pas exportés : ils restent
propres à chaque machine.

Le fichier est chiffré avec un mot de passe choisi à l'export :
- clé tirée du mot de passe par scrypt (lent exprès contre les essais en rafale) ;
- AES-256-GCM par blocs de 1 Mo : chaque bloc est authentifié, un fichier
  modifié ou tronqué est refusé, et un gros fichier n'est jamais chargé
  entièrement en mémoire.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import config

MAGIC = b"BAMBU-CONTROL-BACKUP-1\n"
EXTENSION = ".bcbackup"
FORMAT = 1
CHUNK = 1024 * 1024
MIN_PASSWORD_LENGTH = 8
# Réglages jamais exportés : le compte et la clé de session restent sur chaque machine.
LOCAL_ONLY_SETTINGS = ("admin_username", "admin_password_hash", "secret_key")
MEDIA_FOLDERS = ("media", "photos")


class BackupError(Exception):
    """Message d'erreur lisible, affiché tel quel dans la page."""


# ── Chiffrement ───────────────────────────────────────────
def _key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=32)


def _aad(index: int, last: bool) -> bytes:
    # Le numéro du bloc et « dernier bloc » sont authentifiés : impossible de
    # réordonner, retirer ou couper des blocs sans que ce soit détecté.
    return MAGIC + index.to_bytes(8, "big") + (b"\x01" if last else b"\x00")


def encrypt_file(src: Path, dst: Path, password: str):
    salt, prefix = os.urandom(16), os.urandom(8)
    aes = AESGCM(_key(password, salt))
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC + salt + prefix)
        index, chunk = 0, fin.read(CHUNK)
        while True:
            following = fin.read(CHUNK)
            last = not following
            sealed = aes.encrypt(prefix + index.to_bytes(4, "big"), chunk, _aad(index, last))
            fout.write(len(sealed).to_bytes(4, "big") + sealed)
            if last:
                break
            index, chunk = index + 1, following


def decrypt_file(src: Path, dst: Path, password: str):
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupError("Ce fichier n'est pas une sauvegarde Bambu Control (.bcbackup).")
        salt, prefix = fin.read(16), fin.read(8)
        if len(salt) != 16 or len(prefix) != 8:
            raise BackupError("Fichier de sauvegarde incomplet.")
        aes = AESGCM(_key(password, salt))
        index = 0
        while True:
            size_raw = fin.read(4)
            if len(size_raw) != 4:
                raise BackupError("Fichier de sauvegarde incomplet (coupé pendant la copie ?).")
            sealed = fin.read(int.from_bytes(size_raw, "big"))
            nonce = prefix + index.to_bytes(4, "big")
            for last in (False, True):
                try:
                    fout.write(aes.decrypt(nonce, sealed, _aad(index, last)))
                    break
                except InvalidTag:
                    continue
            else:
                if index == 0:
                    raise BackupError("Mot de passe incorrect (ou fichier abîmé).")
                raise BackupError("Fichier de sauvegarde abîmé : il a été modifié ou mal copié.")
            if last:
                if fin.read(1):
                    raise BackupError("Fichier de sauvegarde abîmé (données en trop).")
                return
            index += 1


# ── Export ────────────────────────────────────────────────
def _db_snapshot(history_db, dst: Path):
    """Copie cohérente de la base, même pendant une impression (API de sauvegarde SQLite)."""
    target = sqlite3.connect(str(dst))
    try:
        with history_db.lock:
            history_db.conn.backup(target)
    finally:
        target.close()


def build_export(history_db, password: str) -> Path:
    """Crée le fichier chiffré dans un dossier temporaire et renvoie son chemin."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise BackupError(f"Le mot de passe de la sauvegarde doit faire au moins {MIN_PASSWORD_LENGTH} caractères.")
    work = Path(tempfile.mkdtemp(prefix="bc-export-"))
    try:
        settings = {}
        if config.SETTINGS_PATH.exists():
            settings = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        for key in LOCAL_ONLY_SETTINGS:
            settings.pop(key, None)
        printers = json.loads(config.PRINTERS_PATH.read_text(encoding="utf-8")) if config.PRINTERS_PATH.exists() else []
        db_copy = work / "history.db"
        _db_snapshot(history_db, db_copy)

        archive = work / "backup.zip"
        media_count = 0
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("settings.json", json.dumps(settings, indent=2, ensure_ascii=False))
            z.writestr("printers.json", json.dumps(printers, indent=2, ensure_ascii=False))
            queue = config.DATA_DIR / "queue.json"
            if queue.exists():
                z.write(queue, "queue.json")
            z.write(db_copy, "history.db")
            for folder in MEDIA_FOLDERS:
                base = config.DATA_DIR / folder
                if not base.is_dir():
                    continue
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        # photos et vidéos déjà compressées : rangées telles quelles (plus rapide)
                        z.write(path, f"{folder}/{path.relative_to(base).as_posix()}", zipfile.ZIP_STORED)
                        media_count += 1
            z.writestr("manifest.json", json.dumps({
                "format": FORMAT,
                "app": "Bambu Control",
                "created": datetime.now().isoformat(timespec="seconds"),
                "printers": len(printers),
                "media_files": media_count,
            }, indent=2))
        out = work / f"bambu-control-{datetime.now():%Y-%m-%d_%H-%M}{EXTENSION}"
        encrypt_file(archive, out, password)
        archive.unlink()
        db_copy.unlink()
        return out
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ── Import ────────────────────────────────────────────────
def _safe_target(base: Path, relative: str) -> Path | None:
    """Chemin dans base/, jamais en dehors (protection contre « ../ » dans l'archive)."""
    target = (base / relative).resolve()
    return target if target.is_relative_to(base.resolve()) and target != base.resolve() else None


def _check_printers(printers) -> list:
    if not isinstance(printers, list):
        raise BackupError("Sauvegarde invalide : liste d'imprimantes illisible.")
    for p in printers:
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p.get(k)
                                              for k in ("id", "name", "ip", "access_code", "serial")):
            raise BackupError("Sauvegarde invalide : une imprimante est incomplète.")
    return printers


def read_backup(encrypted: Path, password: str, work: Path) -> dict:
    """Déchiffre et vérifie la sauvegarde dans work/ ; ne modifie encore rien."""
    archive = work / "backup.zip"
    decrypt_file(encrypted, archive, password)
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise BackupError("Sauvegarde illisible.") from None
    with z:
        names = set(z.namelist())
        if not {"manifest.json", "settings.json", "printers.json"} <= names:
            raise BackupError("Sauvegarde incomplète.")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise BackupError("Sauvegarde faite par une autre version de Bambu Control : mets les deux à jour.")
        settings = json.loads(z.read("settings.json"))
        if not isinstance(settings, dict):
            raise BackupError("Sauvegarde invalide : réglages illisibles.")
        printers = _check_printers(json.loads(z.read("printers.json")))
        queue = json.loads(z.read("queue.json")) if "queue.json" in names else None
        db_path = None
        if "history.db" in names:
            db_path = work / "history.db"
            with z.open("history.db") as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            check = sqlite3.connect(str(db_path))
            try:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("La base de l'historique de la sauvegarde est abîmée.")
            finally:
                check.close()
        media_dir = work / "files"
        media_count = 0
        for name in names:
            folder = name.split("/", 1)[0]
            if folder not in MEDIA_FOLDERS or name.endswith("/"):
                continue
            target = _safe_target(media_dir / folder, name.split("/", 1)[1])
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            media_count += 1
    archive.unlink()
    for key in LOCAL_ONLY_SETTINGS:
        settings.pop(key, None)
    return {"manifest": manifest, "settings": settings, "printers": printers, "queue": queue,
            "db_path": db_path, "media_dir": media_dir, "media_count": media_count}


def restore_database(history_db, source: Path):
    """Remplace tout le contenu de la base ouverte par celui de la sauvegarde."""
    src = sqlite3.connect(str(source))
    try:
        with history_db.lock:
            src.backup(history_db.conn)
    finally:
        src.close()


def restore_media(media_dir: Path):
    """Copie photos, vignettes et timelapses (les fichiers existants du même nom sont remplacés)."""
    for folder in MEDIA_FOLDERS:
        source = media_dir / folder
        if source.is_dir():
            shutil.copytree(source, config.DATA_DIR / folder, dirs_exist_ok=True)
