"""
Mode « appliance » (Raspberry Pi ou autre Linux), piloté depuis
Paramètres → Démarrage :

- démarrage automatique du serveur : service systemd *utilisateur*
  (~/.config/systemd/user), donc sans droits administrateur ;
- plein écran au démarrage de la session : entrée « autostart » qui lance
  le script bambu-kiosk (Chromium en mode kiosk).

Les réglages qui demandent les droits administrateur (connexion automatique
au bureau, veille de l'écran) ne sont pas lancés par l'appli : l'interface
affiche la commande à copier.
"""
import getpass
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import config

SERVICE_NAME = "bambu-control"
DEPLOY_DIR = config.BASE_DIR / "deploy"

# Commandes à lancer soi-même (droits administrateur) — affichées dans l'interface
RASPI_AUTOLOGIN_COMMAND = "sudo raspi-config nonint do_boot_behaviour B4"
RASPI_NO_BLANKING_COMMAND = "sudo raspi-config nonint do_blanking 1"


# ── Emplacements (dans le dossier de l'utilisateur) ───────
def service_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config/systemd/user" / f"{SERVICE_NAME}.service"


def kiosk_script_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".local/bin/bambu-kiosk"


def autostart_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config/autostart/bambu-control-kiosk.desktop"


# ── Détection du système ──────────────────────────────────
def is_linux() -> bool:
    return platform.system() == "Linux"


def has_systemd() -> bool:
    return is_linux() and Path("/run/systemd/system").exists() and shutil.which("systemctl") is not None


def has_desktop() -> bool:
    """Un bureau graphique est installé (Raspberry Pi OS « with desktop », etc.)."""
    return is_linux() and (Path("/usr/share/xsessions").exists() or Path("/usr/share/wayland-sessions").exists())


def find_browser() -> str | None:
    for name in ("chromium-browser", "chromium", "google-chrome"):
        if shutil.which(name):
            return name
    return None


def has_raspi_config() -> bool:
    return shutil.which("raspi-config") is not None


def status(home: Path | None = None) -> dict:
    """Ce que la page Paramètres affiche : possibilités de la machine + état actuel."""
    kiosk_on = autostart_path(home).exists() and "Hidden=false" in autostart_path(home).read_text(encoding="utf-8")
    return {
        "system": platform.system(),
        "can_autostart": has_systemd(),
        "can_kiosk": has_desktop(),
        "browser": find_browser(),
        "raspberry_pi": has_raspi_config(),
        "autostart_enabled": service_path(home).exists(),
        "kiosk_enabled": kiosk_on,
        "user": getpass.getuser(),
        "commands": {"autologin": RASPI_AUTOLOGIN_COMMAND, "no_blanking": RASPI_NO_BLANKING_COMMAND},
    }


# ── Contenu des fichiers ──────────────────────────────────
def render_service(workdir: Path, python: str) -> str:
    template = (DEPLOY_DIR / "bambu-control.service.template").read_text(encoding="utf-8")
    return template.format(workdir=workdir, python=python)


def render_kiosk_script(url: str) -> str:
    return (DEPLOY_DIR / "bambu-kiosk.sh").read_text(encoding="utf-8").replace("__URL__", url)


def render_autostart_entry(kiosk_command: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Bambu Control (kiosk)\n"
        "Comment=Ouvre Bambu Control en plein écran au démarrage\n"
        f"Exec={kiosk_command} start\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Hidden=false\n"
    )


def kiosk_url(port: int) -> str:
    return f"http://localhost:{port}/"


# ── Actions ───────────────────────────────────────────────
def run(command: list[str]) -> tuple[bool, str]:
    """Lance une commande ; renvoie (réussite, message d'erreur éventuel)."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def enable_autostart(runner=run, home: Path | None = None) -> tuple[bool, str]:
    """Le serveur démarrera tout seul à chaque démarrage de la machine."""
    if not has_systemd():
        return False, "Démarrage automatique disponible uniquement sous Linux avec systemd."
    path = service_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_service(config.BASE_DIR, sys.executable), encoding="utf-8", newline="\n")
    for command in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", f"{SERVICE_NAME}.service"]):
        ok, error = runner(command)
        if not ok:
            return False, f"« {' '.join(command)} » a échoué : {error}"
    # « linger » : démarre même sans session ouverte. Souvent autorisé sans mot de passe ;
    # sinon ce n'est pas grave tant que la session s'ouvre automatiquement (mode kiosk).
    linger_ok, _ = runner(["loginctl", "enable-linger", getpass.getuser()])
    if linger_ok:
        return True, "Bambu Control démarrera automatiquement avec la machine."
    return True, ("Bambu Control démarrera à l'ouverture de ta session. Pour qu'il démarre même "
                  f"sans session ouverte : sudo loginctl enable-linger {getpass.getuser()}")


def disable_autostart(runner=run, home: Path | None = None) -> tuple[bool, str]:
    runner(["systemctl", "--user", "disable", f"{SERVICE_NAME}.service"])
    service_path(home).unlink(missing_ok=True)
    runner(["systemctl", "--user", "daemon-reload"])
    return True, "Démarrage automatique désactivé."


def enable_kiosk(port: int, home: Path | None = None) -> tuple[bool, str]:
    """Installe la commande bambu-kiosk et l'ouverture en plein écran à l'ouverture de session."""
    if not has_desktop() and home is None:
        return False, "Aucun bureau graphique détecté sur cette machine."
    script = kiosk_script_path(home)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(render_kiosk_script(kiosk_url(port)), encoding="utf-8", newline="\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    entry = autostart_path(home)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(render_autostart_entry(script), encoding="utf-8", newline="\n")
    message = "Le plein écran s'ouvrira au prochain démarrage de la session."
    if find_browser() is None and home is None:
        message += " Attention : Chromium n'est pas installé (sudo apt install chromium-browser)."
    return True, message


def disable_kiosk(home: Path | None = None) -> tuple[bool, str]:
    autostart_path(home).unlink(missing_ok=True)
    kiosk_script_path(home).unlink(missing_ok=True)
    return True, "Le plein écran ne s'ouvrira plus au démarrage."
