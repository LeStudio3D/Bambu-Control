#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# Bambu Control — désinstallation (installation faite avec deploy/install.sh)
#
#   curl -fsSL https://raw.githubusercontent.com/LeStudio3D/Bambu-Control/main/deploy/uninstall.sh | bash
#
# Enlève le démarrage automatique, le plein écran (kiosk) et le dossier
# ~/Bambu-Control. Une question demande s'il faut garder tes réglages et ton
# historique (dossier data/) : oui par défaut.
# Les paquets du système (Python, ffmpeg, Chromium) restent installés.
# ═══════════════════════════════════════════════════════════
set -uo pipefail

DIR="${BAMBU_DIR:-$HOME/Bambu-Control}"

answer="${BAMBU_KEEP_DATA:-}"
if [ -z "$answer" ] && [ -r /dev/tty ]; then
  read -r -p "Garder tes réglages et ton historique (dossier data/) ? [O/n] " answer </dev/tty || answer=""
fi
case "$(echo "$answer" | tr '[:upper:]' '[:lower:]')" in
  n|non|no|0|false) KEEP_DATA=0 ;;
  *) KEEP_DATA=1 ;;
esac

echo "==> Arrêt du démarrage automatique"
systemctl --user disable --now bambu-control.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/bambu-control.service"
systemctl --user daemon-reload 2>/dev/null || true

echo "==> Arrêt du plein écran (kiosk)"
pkill -f -- "--user-data-dir=$HOME/.config/bambu-control-kiosk" 2>/dev/null || true
rm -f "$HOME/.config/autostart/bambu-control-kiosk.desktop" "$HOME/.local/bin/bambu-kiosk"
rm -rf "$HOME/.config/bambu-control-kiosk"

pkill -f -- "$DIR/app.py" 2>/dev/null || true

echo "==> Suppression de $DIR"
if [ ! -d "$DIR" ]; then
  echo "   (dossier absent)"
elif [ "$KEEP_DATA" = 1 ] && [ -d "$DIR/data" ]; then
  BACKUP="$HOME/Bambu-Control-data-$(date +%Y%m%d-%H%M%S)"
  mv "$DIR/data" "$BACKUP"
  rm -rf "$DIR"
  echo "   Réglages et historique gardés dans : $BACKUP"
  echo "   (pour les reprendre : réinstalle, puis remplace ~/Bambu-Control/data par ce dossier)"
else
  rm -rf "$DIR"
fi

echo
echo "✅ Bambu Control est désinstallé."
