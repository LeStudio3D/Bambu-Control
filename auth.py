"""
Authentification simple à un seul utilisateur, par session Flask.
"""
from functools import wraps

from flask import session, redirect, url_for, request
from werkzeug.security import check_password_hash

import config


def verify_credentials(username: str, password: str) -> bool:
    if username != config.ADMIN_USERNAME:
        return False
    return check_password_hash(config.ADMIN_PASSWORD_HASH, password)


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped
