"""
Crée le compte admin du dashboard : demande un identifiant et un mot de passe,
puis écrit directement les valeurs dans config.py (ADMIN_USERNAME et
ADMIN_PASSWORD_HASH). Pas besoin de copier-coller quoi que ce soit.

Usage :
    python3 setup_admin.py
"""
import getpass
import re
import secrets
from pathlib import Path

from werkzeug.security import generate_password_hash

CONFIG_PATH = Path(__file__).parent / "config.py"


def main():
    if not CONFIG_PATH.exists():
        print(f"❌ Impossible de trouver {CONFIG_PATH}")
        return

    print("── Création du compte admin du dashboard ──\n")

    username = input("Identifiant [admin] : ").strip() or "admin"

    while True:
        password = getpass.getpass("Mot de passe : ")
        password2 = getpass.getpass("Confirme le mot de passe : ")
        if not password:
            print("⚠️  Le mot de passe ne peut pas être vide.\n")
            continue
        if password != password2:
            print("⚠️  Les deux mots de passe ne correspondent pas, réessaie.\n")
            continue
        break

    password_hash = generate_password_hash(password)

    content = CONFIG_PATH.read_text(encoding="utf-8")

    # Remplace ADMIN_USERNAME = ...
    content = re.sub(
        r'ADMIN_USERNAME = .*',
        f'ADMIN_USERNAME = os.environ.get("DASHBOARD_USER", "{username}")',
        content,
    )

    # Remplace ADMIN_PASSWORD_HASH = "..." (peut être sur plusieurs lignes à cause du wrapping)
    content = re.sub(
        r'ADMIN_PASSWORD_HASH = os\.environ\.get\(\s*"DASHBOARD_PASSWORD_HASH",\s*".*?"\s*\)',
        f'ADMIN_PASSWORD_HASH = os.environ.get(\n    "DASHBOARD_PASSWORD_HASH",\n    "{password_hash}"\n)',
        content,
        flags=re.DOTALL,
    )

    # Bonus : si la SECRET_KEY est encore celle par défaut, on en génère une aléatoire
    if 'SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-moi-en-production")' in content:
        random_secret = secrets.token_hex(32)
        content = content.replace(
            'SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-moi-en-production")',
            f'SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "{random_secret}")',
        )
        print("🔑 SECRET_KEY régénérée aléatoirement dans config.py.")

    CONFIG_PATH.write_text(content, encoding="utf-8")

    print(f"\n✅ Compte créé et écrit dans config.py")
    print(f"   Identifiant : {username}")
    print(f"   Mot de passe : (celui que tu viens de taper, non stocké en clair)")
    print(f"\nTu peux maintenant lancer : python3 app.py")


if __name__ == "__main__":
    main()
