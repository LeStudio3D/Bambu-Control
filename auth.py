"""
Authentification (un seul compte admin) et protections associées :
- session Flask
- jeton CSRF pour toutes les requêtes qui modifient quelque chose
- limitation des tentatives de connexion par IP
- redirection après login limitée aux chemins internes
"""
import hmac
import secrets
import threading
import time
from functools import wraps
from urllib.parse import urlparse

from flask import abort, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import settings_store

CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"


# ── Premier lancement ─────────────────────────────────────
# Tant qu'aucun compte n'existe, n'importe qui sur le réseau pourrait le créer.
# Depuis la machine elle-même (écran du Pi en kiosk) c'est autorisé directement ;
# depuis un autre appareil, il faut ce code, affiché dans le terminal du serveur.
SETUP_CODE = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
# Copie du code dans un fichier, lisible seulement par l'utilisateur du Pi : quand Bambu
# Control tourne en service ou en arrière-plan, il n'y a pas de terminal où le lire.
SETUP_CODE_FILE = config.DATA_DIR / "setup_code.txt"
MIN_PASSWORD_LENGTH = 8


def publish_setup_code():
    try:
        SETUP_CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETUP_CODE_FILE.write_text(SETUP_CODE + "\n", encoding="utf-8")
        SETUP_CODE_FILE.chmod(0o600)
    except OSError:
        pass  # le code reste affiché dans le journal


def clear_setup_code():
    try:
        SETUP_CODE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_local_request() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def setup_code_ok(code: str) -> bool:
    return hmac.compare_digest(code.strip().upper(), SETUP_CODE)


def password_problem(password: str, confirmation: str) -> str | None:
    """Message d'erreur si le mot de passe ne convient pas, sinon None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères."
    if password != confirmation:
        return "Les deux mots de passe ne correspondent pas."
    return None


def save_admin(username: str, password: str | None = None):
    """Enregistre l'identifiant et, si fourni, le nouveau mot de passe (hashé)."""
    updates = {"admin_username": username}
    if password is not None:
        updates["admin_password_hash"] = generate_password_hash(password)
    settings_store.save(updates)
    if password is not None:
        clear_setup_code()  # le compte existe : le code ne sert plus


def reset_admin():
    """Secours (mot de passe oublié) : supprime le compte, la création repasse par l'interface."""
    settings_store.save({"admin_password_hash": None})


# ── Mot de passe ──────────────────────────────────────────
def is_admin_configured() -> bool:
    _, password_hash = settings_store.admin_credentials()
    return bool(password_hash)


def verify_credentials(username: str, password: str) -> bool:
    expected_user, password_hash = settings_store.admin_credentials()
    if not password_hash:
        return False
    user_ok = hmac.compare_digest(username, expected_user)
    password_ok = check_password_hash(password_hash, password)
    return user_ok and password_ok


# ── Limitation des tentatives ─────────────────────────────
_failed_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def _recent_failures(ip: str) -> list[float]:
    cutoff = time.time() - config.LOGIN_LOCK_SECONDS
    attempts = [t for t in _failed_attempts.get(ip, []) if t > cutoff]
    _failed_attempts[ip] = attempts
    return attempts


def is_login_locked(ip: str) -> bool:
    with _attempts_lock:
        return len(_recent_failures(ip)) >= config.LOGIN_MAX_ATTEMPTS


def record_login_failure(ip: str):
    with _attempts_lock:
        _recent_failures(ip).append(time.time())


def reset_login_failures(ip: str):
    with _attempts_lock:
        _failed_attempts.pop(ip, None)


# ── Redirection sûre ──────────────────────────────────────
def safe_next_url(candidate: str | None) -> str:
    """N'autorise que les chemins internes ("/history"), jamais un autre site."""
    if not candidate:
        return url_for("index")
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith("/") or candidate.startswith("//"):
        return url_for("index")
    return candidate


# ── CSRF ──────────────────────────────────────────────────
def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def check_csrf():
    """À appeler avant chaque requête : bloque les POST/PUT/DELETE sans jeton valide."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FIELD) or ""
    expected = session.get("csrf_token") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Session expirée, recharge la page."}), 400
        abort(400, "Jeton de sécurité invalide, recharge la page.")


# ── Décorateur ────────────────────────────────────────────
def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Non connecté"}), 401
            if not is_admin_configured():
                return redirect(url_for("setup"))
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped
