// ═══════════════════════════════════════════════════════════
// Bambu Control — home.js (écran d'accueil)
// Tout l'affichage suit l'état envoyé par le serveur (BC.onStatus).
// ═══════════════════════════════════════════════════════════

const $ = id => document.getElementById(id);
const SPEED_LABELS = { 50: "Silencieux", 100: "Standard", 124: "Sport", 166: "Ludicrous" };
let current = { data: {}, connection: "connecting" };

function stateClass(state) {
  if (state === "RUNNING" || state === "PREPARE") return "printing";
  if (state === "PAUSE") return "paused";
  if (state === "FAILED") return "failed";
  return "";
}

function renderState(data, online) {
  const state = data.gcode_state;
  const word = $("state-word");
  word.textContent = online ? (BC.STATE_LABELS[state] || "—") : "Hors ligne";
  word.className = `state-word ${online ? stateClass(state) : "failed"}`;

  const parts = [];
  if (data.print_error) parts.push(`Erreur imprimante ${data.print_error}`);
  if (BC.ACTIVE_STATES.includes(state) && data.print_speed) parts.push(`Vitesse ${SPEED_LABELS[data.print_speed] || data.print_speed + " %"}`);
  if (state === "FINISH") parts.push("Impression terminée — plateau à vider");
  $("state-sub").textContent = parts.join(" · ");

  $("camera-chip-label").textContent = online ? (BC.STATE_LABELS[state] || "—") : "Hors ligne";
  const dot = $("camera-chip").querySelector(".conn-dot");
  dot.style.background = !online ? "var(--danger)" : state === "RUNNING" ? "var(--accent)"
    : state === "PAUSE" ? "var(--warn)" : "var(--faint)";
}

function renderJob(data) {
  const state = data.gcode_state;
  const active = BC.ACTIVE_STATES.includes(state);
  const showJob = active || state === "FINISH" || state === "FAILED";
  const percent = showJob ? (data.mc_percent ?? 0) : 0;

  $("job-file").textContent = showJob && data.file_name ? data.file_name : "Aucune impression";
  $("job-percent").textContent = percent;
  const bar = $("job-progress");
  bar.style.width = `${percent}%`;
  bar.className = `progress-fill ${state === "PAUSE" ? "paused" : state === "FAILED" ? "failed" : ""}`;

  $("job-remaining").textContent = active ? BC.formatMinutes(data.remaining_time_min) : "—";
  $("job-elapsed").textContent = active && data.job ? BC.formatMinutes(BC.minutesSince(data.job.started_at)) : "—";
  $("job-layer").textContent = showJob && data.total_layers
    ? `${data.current_layer ?? 0} / ${data.total_layers}` : "—";
}

function renderTemp(id, value, target) {
  $(id).textContent = value == null ? "—" : Math.round(value);
  const targetEl = $(`${id}-target`);
  if (!targetEl) return;
  targetEl.textContent = target > 0 ? `consigne ${Math.round(target)} °C` : "chauffe arrêtée";
}

function renderTemps(data) {
  renderTemp("t-nozzle", data.nozzle_temp, data.nozzle_target);
  renderTemp("t-bed", data.bed_temp, data.bed_target);
  $("t-chamber-block").hidden = !data.has_chamber_sensor;
  if (data.has_chamber_sensor) renderTemp("t-chamber", data.chamber_temp, null);
}

function renderAms(units) {
  const box = $("ams");
  if (!units || units.length === 0) {
    box.innerHTML = `<div class="ams-empty-msg">Aucun AMS détecté.</div>`;
    $("ams-hint").textContent = "";
    return;
  }
  box.innerHTML = BC.amsUnitsHtml(units);    // tous les AMS, l'un sous l'autre
  const unknown = units.some(u => u.slots.some(s => !s.empty && s.remaining == null));
  $("ams-hint").textContent = unknown ? "Niveau connu seulement pour les bobines Bambu" : "";
}

function renderButtons(data, online) {
  const state = data.gcode_state;
  const busy = BC.isBusy();
  const active = BC.ACTIVE_STATES.includes(state);
  const start = $("btn-start");
  if (state === "PAUSE") {
    $("btn-start-label").textContent = "Reprendre";
    start.disabled = !online || busy;
  } else if (active) {
    $("btn-start-label").textContent = "En cours";
    start.disabled = true;
  } else {
    $("btn-start-label").textContent = "Lancer une impression";
    start.disabled = false;   // mène à la page Impressions
  }
  $("btn-pause").disabled = !online || busy || state !== "RUNNING";
  $("btn-stop").disabled = !online || busy || !active;

  const lightOn = data.light_state === "on";
  const light = $("btn-light");
  light.disabled = !online || busy || !data.light_state;
  light.classList.toggle("is-on", lightOn);
  $("btn-light-label").textContent = lightOn ? "Lumière allumée" : "Lumière éteinte";
}

// ── IA : surveillance et file d'impression ──
function minutesAgo(iso) {
  const minutes = Math.round(BC.minutesSince(iso) || 0);
  return minutes < 1 ? "à l'instant" : `il y a ${minutes} min`;
}

function renderAi(data, online) {
  const ai = data.ai || {};
  const line = $("ai-line");
  $("btn-explain").hidden = !(data.print_error && ai.enabled);
  line.hidden = !(ai.watching && online && BC.ACTIVE_STATES.includes(data.gcode_state));
  if (!line.hidden) {
    const w = ai.watch;
    let cls = "", text = "Surveillance IA active : première vérification après la 2e couche.";
    if (w && w.error) { cls = "is-error"; text = `Surveillance IA : ${w.error}`; }
    else if (w && w.failure) { cls = "is-alert"; text = `IA : problème possible (${w.confidence} %) — ${w.description}`; }
    else if (w) { cls = "is-ok"; text = `Surveillance IA : rien d'anormal (${minutesAgo(w.time)})`; }
    line.className = `ai-line ${cls}`;
    $("ai-line-text").textContent = text;
  }
  const proposal = ai.proposal;
  $("queue-banner").hidden = !(proposal && online);
  if (proposal) {
    const check = proposal.plate_check;
    $("queue-title").textContent = `Prochaine impression prête : ${proposal.name}`;
    $("queue-detail").textContent = !check ? "Vérifie que le plateau est vide avant de lancer."
      : check.error ? `Vérification IA impossible (${check.error}). Vérifie le plateau toi-même.`
      : check.clear ? `Plateau vide d'après l'IA (${check.confidence} %).`
      : `Attention, l'IA voit quelque chose sur le plateau : ${check.description}`;
  }
}

async function queueAction(action) {
  const { ok, json } = await BC.api(`/api/queue/${action}`, { method: "POST", body: { index: 0 } });
  if (json.message) BC.toast(ok ? "ok" : "err", json.message);
  BC.refreshStatus();
}

function render(status) {
  current = status;
  const data = status.data || {};
  const online = status.connection === "online";
  $("home").classList.toggle("is-offline", !online && status.connection === "offline");
  $("unavailable").hidden = status.connection !== "offline";
  $("unavailable-reason").textContent = status.last_error ? `— ${status.last_error}` : "";
  renderState(data, online);
  renderJob(data);
  renderTemps(data);
  renderAms(data.ams_units);
  renderButtons(data, online);
  renderAi(data, online);
}

document.addEventListener("DOMContentLoaded", () => {
  setupCamera($("camera"));
  BC.onStatus(render);
  BC.onBusy(() => render(current));

  $("btn-start").addEventListener("click", () => {
    if (current.data?.gcode_state === "PAUSE") BC.sendCommand("resume");
    else window.location.href = "/prints";
  });
  $("btn-pause").addEventListener("click", () => BC.sendCommand("pause"));
  $("btn-explain").addEventListener("click", () => Assistant.explainError(current.data.print_error));
  $("queue-recheck").addEventListener("click", () => queueAction("recheck"));
  $("queue-skip").addEventListener("click", () => queueAction("remove"));
  $("queue-launch").addEventListener("click", async () => {
    const name = current.data?.ai?.proposal?.name || "le fichier suivant";
    if (!(await BC.confirmDialog({ title: "Lancer l'impression suivante ?", message: `${name} va démarrer. Le plateau est-il bien vide ?`, confirmLabel: "Lancer" }))) return;
    BC.toast("pending", "Lancement en cours, attente de la confirmation de l'imprimante…", { sticky: true });
    const { json } = await BC.api("/api/queue/launch", { method: "POST", body: {} });
    BC.toast(json.status === "confirmed" ? "ok" : json.status === "unconfirmed" ? "warn" : "err", json.message || json.error);
    BC.refreshStatus();
  });
  $("btn-stop").addEventListener("click", () => BC.sendCommand("stop", {}, {
    title: "Arrêter l'impression ?",
    message: "L'impression en cours sera annulée et ne pourra pas être reprise.",
    confirmLabel: "Arrêter l'impression",
  }));
  $("btn-light").addEventListener("click", () =>
    BC.sendCommand("light", { on: current.data?.light_state !== "on" }));
});
