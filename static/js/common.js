// ═══════════════════════════════════════════════════════════
// Bambu Control — common.js
// Outils partagés par toutes les pages : appels API, notifications,
// confirmation, état de l'imprimante en direct, commandes.
// ═══════════════════════════════════════════════════════════

const BC = (() => {
  const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const STATUS_POLL_MS = 2000;

  const STATE_LABELS = {
    IDLE: "Au repos", PREPARE: "Préparation", RUNNING: "Impression",
    PAUSE: "En pause", FINISH: "Terminé", FAILED: "Échec", UNKNOWN: "Inconnu",
  };
  const CONNECTION_LABELS = { online: "Connectée", connecting: "Connexion", offline: "Hors ligne" };
  const ACTIVE_STATES = ["PREPARE", "RUNNING", "PAUSE"];

  // ── Petits utilitaires ──
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function formatMinutes(minutes) {
    if (minutes == null || minutes === "") return "—";
    minutes = Math.max(0, Math.round(minutes));
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return h > 0 ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
  }

  function minutesSince(iso) {
    if (!iso) return null;
    return (Date.now() - new Date(iso).getTime()) / 60000;
  }

  function hexColor(raw, fallback = "4E5867") {
    return /^[0-9A-Fa-f]{6}/.test(raw || "") ? raw.slice(0, 6) : fallback;
  }

  // ── AMS (accueil et page Imprimante) : un bloc par AMS, avec ses emplacements ──
  // spools : bobines du stock posées sur l'imprimante ({emplacement: {remaining_g, low, name}})
  function amsSlotHtml(slot, spools = {}) {
    const spool = spools[slot.label];
    if (slot.empty) {
      return `<div class="ams-slot empty"><span class="ams-num">${escapeHtml(slot.label)}</span>
        <span class="ams-swatch"></span><span class="ams-type">Vide</span><span class="ams-remain">—</span></div>`;
    }
    const remain = slot.remaining != null ? `${slot.remaining} %` : spool ? "" : "niveau inconnu";
    const stock = spool ? `<span class="ams-spool" title="${escapeHtml(spool.name)} (stock)">≈ ${Math.round(spool.remaining_g)} g</span>` : "";
    return `<div class="ams-slot ${slot.active ? "active" : ""} ${spool?.low ? "low" : ""}" title="${slot.active ? "En cours d'utilisation" : ""}">
      <span class="ams-num">${escapeHtml(slot.label)}</span>
      <span class="ams-swatch" style="background:#${hexColor(slot.color)}"></span>
      <span class="ams-type">${escapeHtml(slot.type || "?")}</span>
      ${remain ? `<span class="ams-remain">${remain}</span>` : ""}${stock}</div>`;
  }

  function amsUnitFacts(unit) {
    const facts = [];
    if (unit.humidity_pct != null) facts.push(`Humidité ${unit.humidity_pct} %`);
    else if (unit.humidity_level != null) facts.push(`Humidité ${unit.humidity_level}/5`);
    if (unit.temp != null && unit.temp > 0) facts.push(`${unit.temp} °C`);
    if (unit.drying) {
      facts.push(`<strong class="ams-drying">Séchage${unit.dry_temp ? " " + unit.dry_temp + " °C" : ""}` +
        `${unit.dry_filament ? " " + escapeHtml(unit.dry_filament) : ""} · reste ${formatMinutes(unit.dry_remaining_min)}</strong>`);
    }
    return facts.join(" · ");
  }

  function amsSlotsHtml(slots, spools = {}) {
    return `<div class="ams">${slots.map(s => amsSlotHtml(s, spools)).join("")}</div>`;
  }

  function amsUnitsHtml(units, spools = {}) {
    return units.map(unit => `
      <div class="ams-unit">
        <div class="ams-unit-head"><strong>${escapeHtml(unit.name)}</strong>
          <span class="muted">${escapeHtml(unit.model)}</span>
          <span class="ams-facts">${amsUnitFacts(unit)}</span></div>
        ${amsSlotsHtml(unit.slots, spools)}
      </div>`).join("");
  }

  // ── Appels API ──
  async function api(url, { method = "GET", body } = {}) {
    const options = { method, headers: {} };
    if (method !== "GET") options.headers["X-CSRF-Token"] = CSRF_TOKEN;
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const res = await fetch(url, options);
    if (res.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      throw new Error("Session expirée");
    }
    let json = {};
    try { json = await res.json(); } catch (e) { /* réponse vide ou non JSON */ }
    return { ok: res.ok, status: res.status, json };
  }

  // ── Tâches IA longues : lancement puis suivi toutes les 1,5 s ──
  // Aucune requête ne reste ouverte longtemps : une coupure réseau passagère
  // ne fait pas échouer une réponse qui prend plusieurs minutes.
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function aiTask(url, body, onProgress) {
    const first = await api(url, { method: "POST", body });
    if (first.status !== 202) return first;              // erreur immédiate (IA désactivée, saisie invalide…)
    const taskUrl = `/api/ai/tasks/${first.json.task}`;
    let failures = 0;
    for (;;) {
      await sleep(1500);
      let res;
      try {
        res = await api(taskUrl);
        failures = 0;
      } catch (e) {
        if (++failures >= 20) {
          return { ok: false, json: { error: "Connexion avec Bambu Control perdue pendant la réponse de l'IA." } };
        }
        continue;
      }
      if (res.json.status === "done") return { ok: true, json: res.json.result };
      if (res.json.status === "error" || !res.ok) return { ok: false, json: { error: res.json.error } };
      if (onProgress) onProgress(res.json.elapsed);
    }
  }

  // ── Notification en bas de l'écran ──
  let toastTimer = null;
  function toast(type, message, { sticky = false } = {}) {
    const el = document.getElementById("toast");
    el.className = `toast ${type}`;
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toastTimer);
    if (!sticky) toastTimer = setTimeout(() => { el.hidden = true; }, type === "ok" ? 4000 : 9000);
  }

  // ── Fenêtre de confirmation (remplace window.confirm, utilisable au tactile) ──
  function confirmDialog({ title, message, confirmLabel = "Confirmer" }) {
    const dialog = document.getElementById("confirm-dialog");
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-message").textContent = message;
    document.getElementById("confirm-ok").textContent = confirmLabel;
    return new Promise(resolve => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
      dialog.showModal();
    });
  }

  // ── Commandes (le serveur attend la confirmation de l'imprimante) ──
  const COMMAND_TOAST = { confirmed: "ok", unconfirmed: "warn", rejected: "err", error: "err" };
  let commandBusy = false;
  const busyListeners = [];

  function setBusy(value) {
    commandBusy = value;
    busyListeners.forEach(fn => fn(value));
  }

  async function sendCommand(name, body = {}, confirmation = null) {
    if (commandBusy) return null;
    if (confirmation && !(await confirmDialog(confirmation))) return null;
    setBusy(true);
    toast("pending", "Commande envoyée, en attente de la confirmation de l'imprimante…", { sticky: true });
    try {
      const { json } = await api(`/api/command/${name}`, { method: "POST", body });
      const status = json.status || "error";
      toast(COMMAND_TOAST[status] || "err", json.message || json.error || "Erreur inconnue");
      return status;
    } catch (e) {
      toast("err", `Serveur Bambu Control injoignable (${e.message}).`);
      return "error";
    } finally {
      setBusy(false);
      refreshStatus();
    }
  }

  // ── État de l'imprimante en direct ──
  const statusListeners = [];
  let lastStatus = null;

  function updateConnectionIndicator(connection, lastError) {
    const el = document.getElementById("conn");
    if (!el) return;
    el.className = `conn ${connection}`;
    document.getElementById("conn-label").textContent = CONNECTION_LABELS[connection] || "Hors ligne";
    el.title = lastError || "";
  }

  async function refreshStatus() {
    if (document.hidden) return;
    try {
      const { json } = await api("/api/status");
      lastStatus = json;
      updateConnectionIndicator(json.connection, json.last_error);
    } catch (e) {
      lastStatus = { connection: "offline", connected: false, last_error: "Bambu Control ne répond pas", data: {} };
      updateConnectionIndicator("offline", lastStatus.last_error);
    }
    statusListeners.forEach(fn => fn(lastStatus));
  }

  function onStatus(fn) {
    statusListeners.push(fn);
    if (lastStatus) fn(lastStatus);
  }

  function startClock() {
    const el = document.getElementById("clock");
    const tick = () => { el.textContent = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }); };
    tick();
    setInterval(tick, 10000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    startClock();
    if (document.getElementById("conn")) {
      refreshStatus();
      setInterval(refreshStatus, STATUS_POLL_MS);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
    }
  });

  return {
    CSRF_TOKEN, STATE_LABELS, ACTIVE_STATES,
    escapeHtml, formatMinutes, minutesSince, hexColor, amsUnitsHtml, amsSlotsHtml, amsUnitFacts,
    api, aiTask, toast, confirmDialog, sendCommand, onStatus, refreshStatus,
    currentStatus: () => lastStatus,
    onBusy: fn => busyListeners.push(fn),
    isBusy: () => commandBusy,
  };
})();
