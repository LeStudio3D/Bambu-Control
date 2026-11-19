#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# Bambu Control — mode kiosk (Chromium plein écran)
#
#   bambu-kiosk start     ouvre Bambu Control en plein écran
#   bambu-kiosk stop      ferme le kiosk (maintenance)
#   bambu-kiosk disable   ne plus l'ouvrir au démarrage
#   bambu-kiosk enable    l'ouvrir de nouveau au démarrage
#   bambu-kiosk status    affiche l'état
#
# Raccourci clavier pour sortir du plein écran : Alt+F4 (ferme le kiosk).
# ═══════════════════════════════════════════════════════════
set -u

URL="${BAMBU_URL:-__URL__}"
PROFILE="$HOME/.config/bambu-control-kiosk"
AUTOSTART="$HOME/.config/autostart/bambu-control-kiosk.desktop"
WAIT_SECONDS=90

find_browser() {
  for name in chromium-browser chromium google-chrome; do
    if command -v "$name" >/dev/null 2>&1; then echo "$name"; return 0; fi
  done
  return 1
}

wait_for_server() {
  # Le serveur peut démarrer après la session graphique : on l'attend.
  for _ in $(seq 1 "$WAIT_SECONDS"); do
    if curl -fsS -o /dev/null "${URL%/}/login" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

start() {
  local browser
  if ! browser=$(find_browser); then
    echo "Chromium introuvable. Installe-le avec : sudo apt install chromium-browser" >&2
    exit 1
  fi
  if ! wait_for_server; then
    echo "Bambu Control ne répond pas sur $URL (on ouvre quand même, la page se rechargera)." >&2
  fi
  # Après une coupure de courant, Chromium afficherait « ne s'est pas fermé correctement » :
  # on marque la dernière session comme fermée proprement.
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"Crashed"/"exit_type":"Normal"/' \
    "$PROFILE/Default/Preferences" 2>/dev/null || true
  exec "$browser" \
    --kiosk "$URL" \
    --user-data-dir="$PROFILE" \
    --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
    --no-first-run --check-for-update-interval=31536000 \
    --overscroll-history-navigation=0 --password-store=basic
}

stop() {
  if pkill -f -- "--user-data-dir=$PROFILE"; then
    echo "Kiosk fermé. Pour le relancer : bambu-kiosk start"
  else
    echo "Le kiosk n'était pas ouvert."
  fi
}

set_autostart() {
  if [ ! -f "$AUTOSTART" ]; then
    echo "Plein écran non installé : active-le dans Bambu Control → Paramètres → Démarrage." >&2
    exit 1
  fi
  sed -i "s/^Hidden=.*/Hidden=$1/" "$AUTOSTART"
}

status() {
  if pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null; then echo "Kiosk : ouvert"; else echo "Kiosk : fermé"; fi
  if [ -f "$AUTOSTART" ] && grep -q "^Hidden=false" "$AUTOSTART"; then
    echo "Au démarrage : activé"
  else
    echo "Au démarrage : désactivé"
  fi
  echo "Adresse : $URL"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  enable) set_autostart false && echo "Le kiosk s'ouvrira au prochain démarrage." ;;
  disable) set_autostart true && echo "Le kiosk ne s'ouvrira plus au démarrage (bambu-kiosk enable pour revenir)." ;;
  status) status ;;
  *) echo "Usage : bambu-kiosk start | stop | enable | disable | status"; exit 1 ;;
esac
