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
#   4. active, si tu le veux, le démarrage automatique et le plein écran (kiosk),
#      puis lance Bambu Control tout de suite
#
# Le script pose deux questions (oui par défaut). Pour répondre d'avance :
#   curl -fsSL .../install.sh | BAMBU_AUTOSTART=non BAMBU_KIOSK=non bash
# Désinstaller : curl -fsSL .../deploy/uninstall.sh | bash
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

# Question oui/non. Lit le clavier même quand le script arrive par « curl | bash ».
ask() {
  local preset="$1" question="$2" answer=""
  if [ -n "$preset" ]; then answer="$preset"
  elif [ -r /dev/tty ]; then
    read -r -p "$question [O/n] " answer </dev/tty || answer=""
  fi
  case "$(echo "$answer" | tr '[:upper:]' '[:lower:]')" in
    n|non|no|0|false) return 1 ;;
    *) return 0 ;;
  esac
}

echo "Bambu Control — installation"
echo
if ask "${BAMBU_AUTOSTART:-}" "Démarrer Bambu Control automatiquement avec le Raspberry Pi ?"; then
  AUTOSTART=1; else AUTOSTART=0; fi
if ask "${BAMBU_KIOSK:-}" "Ouvrir la console en plein écran (kiosk) sur l'écran du Pi au démarrage ?"; then
  KIOSK=1; else KIOSK=0; fi

step "1/4 Paquets du système (le mot de passe peut être demandé)"
sudo apt-get update -qq
PACKAGES="git python3 python3-venv python3-pip ffmpeg curl"
if [ "$KIOSK" = 1 ]; then
  if apt-cache show chromium >/dev/null 2>&1; then PACKAGES="$PACKAGES chromium"
  elif apt-cache show chromium-browser >/dev/null 2>&1; then PACKAGES="$PACKAGES chromium-browser"; fi
fi
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

step "4/4 Démarrage de Bambu Control"
cd "$DIR"
OPTIONS=""
[ "$AUTOSTART" = 1 ] || OPTIONS="$OPTIONS --no-autostart"
[ "$KIOSK" = 1 ] || OPTIONS="$OPTIONS --no-kiosk"
"$DIR/.venv/bin/python" app.py --install $OPTIONS

PORT=$("$DIR/.venv/bin/python" -c "import settings_store; print(settings_store.load()['port'])" 2>/dev/null || echo 5000)
LOG="$DIR/data/bambu-control.log"

# On arrête une version déjà lancée (ancienne installation, lancement à la main)
# pour que la nouvelle démarre à coup sûr.
systemctl --user stop bambu-control.service 2>/dev/null || true
pkill -f -- "$DIR/app.py" 2>/dev/null || true
sleep 1

# Lancement tout de suite, dans les deux cas :
#  - démarrage automatique choisi : par le service (il redémarrera aussi avec le Pi) ;
#  - sinon : en arrière-plan, jusqu'au prochain redémarrage du Pi.
if [ "$AUTOSTART" = 1 ] && systemctl --user start bambu-control.service 2>/dev/null; then
  STARTED_BY=service
else
  STARTED_BY=manual
  setsid nohup "$DIR/.venv/bin/python" "$DIR/app.py" >"$LOG" 2>&1 < /dev/null &
fi

RUNNING=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/login" 2>/dev/null; then RUNNING=1; break; fi
  sleep 1
done

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
if [ "$RUNNING" = 1 ]; then
  echo "✅ Bambu Control est installé et lancé."
else
  echo "⚠️  Bambu Control est installé mais ne répond pas encore sur le port ${PORT}."
  if [ "$STARTED_BY" = service ]; then
    echo "   Pour voir pourquoi : journalctl --user -u bambu-control -n 30"
  else
    echo "   Pour voir pourquoi : tail -n 30 $LOG"
  fi
  echo "   (Une autre copie de Bambu Control utilise peut-être déjà le port ${PORT} : arrête-la.)"
fi
cat <<EOF

   Ouvrir la console : http://localhost:${PORT} (sur le Pi)
                       http://${IP:-adresse-du-pi}:${PORT} (depuis un autre appareil)

   Au premier lancement, la page web demande de créer le compte administrateur,
   puis d'ajouter ton imprimante (IP, code d'accès, numéro de série).

EOF
if [ "$AUTOSTART" = 1 ]; then
  cat <<EOF
   Démarrage automatique : oui (Bambu Control redémarre tout seul avec le Pi).
     Arrêter  : systemctl --user stop bambu-control
     Relancer : systemctl --user start bambu-control
     État     : systemctl --user status bambu-control
EOF
else
  cat <<EOF
   Démarrage automatique : non (Bambu Control tourne jusqu'au prochain redémarrage du Pi).
     Arrêter celui lancé par l'installation : pkill -f "$DIR/app.py"
     Relancer : cd $DIR && .venv/bin/python app.py
                (laisse le terminal ouvert ; Ctrl+C dans ce terminal pour l'arrêter)
EOF
fi
if [ "$KIOSK" = 1 ]; then
  cat <<EOF

   Plein écran : il s'ouvrira au prochain démarrage (sudo reboot).
     Ouvrir maintenant : ~/.local/bin/bambu-kiosk start
     Fermer            : Alt+F4 (ou ~/.local/bin/bambu-kiosk stop)
EOF
fi
cat <<EOF

   Mise à jour ou changement de choix : relance la même commande d'installation.
   Désinstaller : curl -fsSL https://raw.githubusercontent.com/LeStudio3D/Bambu-Control/main/deploy/uninstall.sh | bash
EOF
