#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# Bambu Control — installation en une commande (Raspberry Pi OS / Debian / Ubuntu)
#
#   curl -fsSL https://raw.githubusercontent.com/LeStudio3D/Bambu-Control/main/deploy/install.sh | bash
#
# Ce script :
#   1. installe les paquets nécessaires (Python, ffmpeg pour les timelapses, Chromium)
#   2. télécharge Bambu Control dans ~/Bambu-Control (ou le met à jour s'il existe)
#   3. crée l'environnement Python et installe les dépendances
#   4. active le démarrage automatique et le plein écran (console)
#
# Tes réglages (dossier data/) ne sont jamais touchés par une mise à jour.
# Relancer le script plus tard = mettre Bambu Control à jour.
# ═══════════════════════════════════════════════════════════
set -euo pipefail

REPO="${BAMBU_REPO:-https://github.com/LeStudio3D/Bambu-Control.git}"
DIR="${BAMBU_DIR:-$HOME/Bambu-Control}"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$1"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "Lance ce script avec ton utilisateur normal (pas en root, pas avec sudo) :" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/LeStudio3D/Bambu-Control/main/deploy/install.sh | bash" >&2
  exit 1
fi

step "1/4 Paquets du système (le mot de passe peut être demandé)"
sudo apt-get update -qq
PACKAGES="git python3 python3-venv python3-pip ffmpeg curl"
if apt-cache show chromium >/dev/null 2>&1; then PACKAGES="$PACKAGES chromium"
elif apt-cache show chromium-browser >/dev/null 2>&1; then PACKAGES="$PACKAGES chromium-browser"; fi
sudo apt-get install -y -qq $PACKAGES

step "2/4 Téléchargement de Bambu Control dans $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

step "3/4 Environnement Python"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

step "4/4 Démarrage automatique et plein écran"
cd "$DIR"
"$DIR/.venv/bin/python" app.py --install
systemctl --user restart bambu-control.service 2>/dev/null || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PORT=$("$DIR/.venv/bin/python" -c "import settings_store; print(settings_store.load()['port'])" 2>/dev/null || echo 5000)
cat <<EOF

✅ Bambu Control est installé.

   Sur cet écran : redémarre le Raspberry Pi (sudo reboot), la console s'ouvrira en plein écran.
   Depuis un autre appareil : http://${IP:-adresse-du-pi}:${PORT}

   Au premier lancement, la page web demande de créer le compte administrateur,
   puis d'ajouter ton imprimante (IP, code d'accès, numéro de série).

   Mise à jour plus tard : relance simplement la même commande d'installation.
EOF
