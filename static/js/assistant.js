// ═══════════════════════════════════════════════════════════
// Bambu Control — assistant.js
// Panneau latéral : discussion avec l'assistant IA + alertes.
// Les actions proposées par l'IA ne partent qu'après un clic de
// l'utilisateur ET une confirmation.
// ═══════════════════════════════════════════════════════════

const Assistant = (() => {
  const STORAGE_KEY = "bc-chat";
  const EVENTS_POLL_MS = 20000;
  const LEVEL_LABELS = { alert: "Alerte", warn: "Attention", info: "Info", ok: "RAS" };
  const HIDE_OK_KEY = "bc-hide-ok";
  let lastEvents = [];
  let messages = [];
  let busy = false;

  const $ = id => document.getElementById(id);

  // ── Ouverture / onglets ──
  function open(tab = "chat") {
    $("drawer").hidden = false;
    showTab(tab);
  }

  function close() { $("drawer").hidden = true; }

  function showTab(tab) {
    document.querySelectorAll("#drawer .tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
    document.querySelectorAll("#drawer [data-panel]").forEach(p => { p.hidden = p.dataset.panel !== tab; });
    if (tab === "events") loadEvents(true);
    if (tab === "chat") $("chat-input").focus();
  }

  // ── Discussion ──
  function save() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-30))); } catch (e) { /* stockage indisponible */ }
  }

  function restore() {
    try { messages = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]"); } catch (e) { messages = []; }
  }

  function renderChat() {
    const box = $("chat");
    if (!messages.length) {
      box.innerHTML = `<div class="chat-empty">
        <strong>Assistant Bambu Control</strong>
        <span>Exemples : « Combien de temps reste-t-il ? », « Quelle bobine est utilisée ? »,
        « Allume la lumière », « Pourquoi mes impressions échouent ? »</span></div>`;
      return;
    }
    box.innerHTML = messages.map((m, i) => {
      const actions = (m.actions || []).map((a, j) =>
        `<button type="button" class="btn btn-small" data-action="${i}:${j}">${BC.escapeHtml(a.label)}</button>`).join("");
      return `<div class="bubble ${m.role}${m.error ? " error" : ""}">${BC.escapeHtml(m.content).replace(/\n/g, "<br>")}
        ${actions ? `<div class="bubble-actions">${actions}</div>` : ""}</div>`;
    }).join("") + (busy ? `<div class="bubble assistant pending" id="chat-pending">L'assistant réfléchit…</div>` : "");
    box.scrollTop = box.scrollHeight;
  }

  function showProgress(seconds) {
    const el = $("chat-pending");
    if (el) el.textContent = `L'assistant réfléchit… ${seconds} s` +
      (seconds > 20 ? " (normal si l'IA tourne sur un ordinateur sans carte graphique)" : "");
  }

  function errorText(e) {
    return `Bambu Control ne répond plus (${e.message}). Vérifie que le serveur tourne, puis réessaie.`;
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    messages.push({ role: "user", content: text });
    busy = true;
    renderChat();
    try {
      const history = messages.filter(m => !m.error).map(m => ({ role: m.role, content: m.content }));
      const { ok, json } = await BC.aiTask("/api/ai/chat", { messages: history }, showProgress);
      if (ok) messages.push({ role: "assistant", content: json.reply || "(pas de réponse)", actions: json.actions || [] });
      else messages.push({ role: "assistant", content: json.error || "Erreur inconnue", error: true });
    } catch (e) {
      messages.push({ role: "assistant", content: errorText(e), error: true });
    } finally {
      busy = false;
      save();
      renderChat();
    }
  }

  async function runAction(ref) {
    const [i, j] = ref.split(":").map(Number);
    const action = messages[i]?.actions?.[j];
    if (!action) return;
    await BC.sendCommand(action.command, action.params, {
      title: "Exécuter cette action ?",
      message: `L'assistant propose : ${action.label}.`,
      confirmLabel: "Exécuter",
    });
  }

  // Explication d'un code d'erreur (bouton « Expliquer » des pages)
  async function explainError(code) {
    open("chat");
    messages.push({ role: "user", content: `Que signifie l'erreur ${code} ?` });
    busy = true;
    renderChat();
    try {
      const { ok, json } = await BC.aiTask("/api/ai/explain", { code }, showProgress);
      messages.push(ok ? { role: "assistant", content: `Explication probable (IA, à vérifier sur le wiki Bambu Lab) :
${json.explanation}` }
                       : { role: "assistant", content: json.error || "Erreur inconnue", error: true });
    } catch (e) {
      messages.push({ role: "assistant", content: errorText(e), error: true });
    } finally {
      busy = false;
      save();
      renderChat();
    }
  }

  // ── Alertes ──
  function formatTime(iso) {
    const d = new Date(iso);
    return `${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
  }

  function setBadges(count) {
    ["assistant-badge", "events-badge"].forEach(id => {
      const el = $(id);
      el.hidden = !count;
      el.textContent = count > 99 ? "99+" : count;
    });
  }

  async function loadEvents(markRead = false) {
    try {
      const { ok, json } = await BC.api("/api/ai/events?limit=80");
      if (!ok) return;
      setBadges(markRead ? 0 : json.unread);
      if (markRead || !$("drawer").hidden) renderEvents(json.events);
      if (markRead && json.unread) BC.api("/api/ai/events/read", { method: "POST", body: {} });
    } catch (e) { /* on réessaie plus tard */ }
  }

  // ── Effacement ──
  async function clearChat() {
    if (busy || !messages.length) return;
    const ok = await BC.confirmDialog({ title: "Effacer la discussion ?",
      message: "Tous les messages avec l'assistant seront supprimés.", confirmLabel: "Effacer" });
    if (!ok) return;
    messages = [];
    save();
    renderChat();
  }

  async function deleteEvents(body, confirmation) {
    if (confirmation && !(await BC.confirmDialog(confirmation))) return;
    try {
      const { ok, json } = await BC.api("/api/ai/events/delete", { method: "POST", body });
      if (!ok) BC.toast("err", json.error || "Effacement impossible");
    } catch (e) {
      BC.toast("err", `Bambu Control ne répond pas (${e.message}).`);
    }
    loadEvents(true);
  }

  // Photo + analyse IA immédiate (marche aussi hors impression)
  async function watchNow() {
    const btn = $("watch-now");
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "Photo + analyse IA…";
    try {
      const { ok, json } = await BC.aiTask("/api/ai/watch_now", {}, s => { btn.textContent = `Analyse IA… ${s} s`; });
      if (!ok) BC.toast("err", json.error || "Contrôle impossible");
      else if (json.error) BC.toast("warn", json.error);
      else BC.toast("ok", "Contrôle terminé : résultat ajouté aux alertes.");
    } catch (e) {
      BC.toast("err", `Bambu Control ne répond pas (${e.message}).`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Contrôler maintenant";
      loadEvents(true);
    }
  }

  function hideOk() {
    try { return localStorage.getItem(HIDE_OK_KEY) === "1"; } catch (e) { return false; }
  }

  function renderEvents(events) {
    lastEvents = events;
    const box = $("events");
    if (hideOk()) events = events.filter(e => e.level !== "ok");
    if (!events.length) {
      box.innerHTML = `<div class="muted">Aucune alerte pour l'instant.</div>`;
      return;
    }
    box.innerHTML = events.map(e => `
      <div class="event ${e.level}${e.read ? "" : " unread"}">
        <div class="event-head"><span class="event-level">${LEVEL_LABELS[e.level] || "Info"}</span>
          <span class="muted">${formatTime(e.timestamp)}${e.printer_name ? " · " + BC.escapeHtml(e.printer_name) : ""}
            <button type="button" class="event-delete" data-delete="${e.id}" title="Effacer cette alerte" aria-label="Effacer cette alerte">✕</button></span></div>
        <strong>${BC.escapeHtml(e.title)}</strong>
        ${e.message ? `<p>${BC.escapeHtml(e.message)}</p>` : ""}
        ${e.photo_url ? `<a href="${e.photo_url}" target="_blank" rel="noopener"><img src="${e.photo_url}" alt="Photo de la caméra" loading="lazy"></a>` : ""}
      </div>`).join("");
  }

  document.addEventListener("DOMContentLoaded", () => {
    restore();
    renderChat();
    $("assistant-open").addEventListener("click", () => ($("drawer").hidden ? open() : close()));
    $("drawer-close").addEventListener("click", close);
    document.querySelectorAll("#drawer .tab").forEach(t => t.addEventListener("click", () => showTab(t.dataset.tab)));
    $("chat-form").addEventListener("submit", e => {
      e.preventDefault();
      const input = $("chat-input");
      send(input.value);
      input.value = "";
    });
    $("chat").addEventListener("click", e => {
      const btn = e.target.closest("[data-action]");
      if (btn) runAction(btn.dataset.action);
    });
    $("watch-now").addEventListener("click", watchNow);
    $("chat-clear").addEventListener("click", clearChat);
    $("events-clear").addEventListener("click", () => deleteEvents({ all: true }, {
      title: "Effacer toutes les alertes ?",
      message: "Les alertes et leurs photos seront supprimées. Les photos de fin d'impression restent dans l'historique.",
      confirmLabel: "Tout effacer" }));
    $("events").addEventListener("click", e => {
      const btn = e.target.closest("[data-delete]");
      if (btn) deleteEvents({ id: Number(btn.dataset.delete) });
    });
    const hideOkBox = $("events-hide-ok");
    hideOkBox.checked = hideOk();
    hideOkBox.addEventListener("change", () => {
      try { localStorage.setItem(HIDE_OK_KEY, hideOkBox.checked ? "1" : "0"); } catch (e) { /* stockage indisponible */ }
      renderEvents(lastEvents);
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("drawer").hidden) close(); });
    loadEvents();
    setInterval(() => { if (!document.hidden) loadEvents(!$("drawer").hidden && !document.querySelector('[data-panel="events"]').hidden); }, EVENTS_POLL_MS);
  });

  return { open, send, explainError };
})();
