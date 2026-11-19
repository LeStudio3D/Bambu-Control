// ═══════════════════════════════════════════════════════════
// Bambu Control — camera.js
// Une seule requête à la fois : l'image suivante n'est demandée qu'une
// fois la précédente arrivée. Caméra indisponible → la raison s'affiche
// et on réessaie plus lentement. Rien n'est chargé si l'onglet est masqué.
// ═══════════════════════════════════════════════════════════

const CAMERA_FRAME_MS = 500;   // ~2 images/s : suffisant pour surveiller
const CAMERA_RETRY_MS = 3000;

function setupCamera(root) {
  const img = root.querySelector("img");
  const message = root.querySelector(".camera-message");
  const messageText = message.querySelector("strong");
  let objectUrl = null;

  function showMessage(text) {
    img.hidden = true;
    message.hidden = false;
    messageText.textContent = text || "Caméra indisponible";
  }

  async function loadFrame() {
    const res = await fetch(`/api/camera.jpg?t=${Date.now()}`, { cache: "no-store" });
    if (res.status === 401) { window.location.href = "/login"; return false; }
    if (!res.ok) {
      let info = {};
      try { info = await res.json(); } catch (e) { /* pas de JSON */ }
      showMessage(info.message);
      return false;
    }
    const blob = await res.blob();
    const previous = objectUrl;
    objectUrl = URL.createObjectURL(blob);
    img.src = objectUrl;
    img.hidden = false;
    message.hidden = true;
    if (previous) URL.revokeObjectURL(previous);  // libère la mémoire de l'image précédente
    return true;
  }

  async function loop() {
    let delay = CAMERA_FRAME_MS;
    if (!document.hidden) {
      try {
        delay = (await loadFrame()) ? CAMERA_FRAME_MS : CAMERA_RETRY_MS;
      } catch (e) {
        showMessage("Bambu Control ne répond pas");
        delay = CAMERA_RETRY_MS;
      }
    }
    setTimeout(loop, delay);
  }

  // Plein écran : API native, sinon agrandissement CSS (certains navigateurs
  // en mode kiosk refusent le plein écran sans rien dire).
  function toggleFullscreen() {
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    if (root.classList.contains("is-fullscreen")) { root.classList.remove("is-fullscreen"); return; }
    const fallback = () => root.classList.add("is-fullscreen");
    if (!root.requestFullscreen) { fallback(); return; }
    root.requestFullscreen().catch(fallback);
    setTimeout(() => { if (!document.fullscreenElement) fallback(); }, 400);
  }

  root.querySelector("[data-fullscreen]")?.addEventListener("click", toggleFullscreen);
  root.addEventListener("dblclick", toggleFullscreen);
  document.addEventListener("keydown", e => { if (e.key === "Escape") root.classList.remove("is-fullscreen"); });
  loop();
}
