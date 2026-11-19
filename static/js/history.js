// ═══════════════════════════════════════════════════════════
// Bambu Control — history.js (page Historique)
// Les données sont chargées à l'ouverture puis toutes les minutes :
// la base SQLite n'est jamais interrogée en continu.
// ═══════════════════════════════════════════════════════════

const REFRESH_MS = 60000;

// Palette validée (contraste, daltonisme) sur le fond sombre des panneaux
const TEMPERATURE_SERIES = [
  { key: "nozzle_temp", label: "Buse", color: "#d95926" },
  { key: "bed_temp", label: "Plateau", color: "#3987e5" },
  { key: "chamber_temp", label: "Chambre", color: "#199e70" },
];
const PROGRESS_SERIES = [{ key: "mc_percent", label: "Progression", color: "#3987e5" }];

const PRINTER_STATE_LABELS = {
  IDLE: "Au repos", PREPARE: "Préparation", RUNNING: "Impression",
  PAUSE: "En pause", FINISH: "Terminé", FAILED: "Échec",
};
const STATUS_ICONS = { finished: "✓", failed: "✕", stopped: "■", interrupted: "!", running: "●" };

let selectedJob = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })} ` +
         d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function filters() {
  return {
    printer: document.getElementById("filter-printer").value,
    hours: parseInt(document.getElementById("filter-period").value, 10),
  };
}

async function getJSON(url) {
  const res = await fetch(url);
  if (res.status === 401) { window.location.href = "/login"; throw new Error("Session expirée"); }
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || "Erreur serveur");
  return json;
}

// ── Effacement de tout l'historique (imprimante choisie, ou toutes) ──
async function clearHistory() {
  const select = document.getElementById("filter-printer");
  const all = select.value === "all";
  const name = select.selectedOptions[0].textContent.trim();
  const ok = await BC.confirmDialog({
    title: all ? "Effacer l'historique de toutes les imprimantes ?" : `Effacer tout l'historique de « ${name} » ?`,
    message: "Impressions, courbes, erreurs et photos de fin d'impression seront supprimées définitivement. " +
             "Une impression en cours est conservée. Les alertes de l'assistant ne sont pas touchées.",
    confirmLabel: "Tout effacer",
  });
  if (!ok) return;
  try {
    const { ok: done, json } = await BC.api("/api/history/clear", { method: "POST", body: { printer: select.value } });
    BC.toast(done ? "ok" : "err", json.message || json.error || "Effacement impossible");
  } catch (e) {
    BC.toast("err", `Bambu Control ne répond pas (${e.message}).`);
  }
  selectedJob = null;
  refreshAll();
}

// ── Chiffres clés ──
async function loadStats() {
  const { printer, hours } = filters();
  const days = Math.max(1, Math.round(hours / 24));
  const s = await getJSON(`/api/history/stats?printer=${printer}&days=${days}`);
  document.getElementById("stat-jobs").textContent = s.jobs;
  document.getElementById("stat-success").textContent = s.success_rate == null ? "—" : `${s.success_rate} %`;
  document.getElementById("stat-time").textContent = formatDuration(s.print_seconds);
  document.getElementById("stat-failed").textContent = s.failed + s.stopped;
}

// ── Courbes ──
async function loadCharts() {
  const { printer, hours } = filters();
  const tempBox = document.getElementById("chart-temperatures");
  const progressBox = document.getElementById("chart-progress");
  if (printer === "all") {
    const msg = `<div class="chart-empty">Choisis une imprimante pour afficher ses courbes.</div>`;
    tempBox.innerHTML = msg;
    progressBox.innerHTML = msg;
    return;
  }
  const query = selectedJob ? `job=${selectedJob.id}` : `hours=${hours}`;
  const rows = await getJSON(`/api/history/samples?printer=${printer}&${query}`);
  const hasChamber = rows.some(r => r.chamber_temp != null);
  drawLineChart(tempBox, {
    rows, unit: "°",
    series: TEMPERATURE_SERIES.filter(s => s.key !== "chamber_temp" || hasChamber),
  });
  drawLineChart(progressBox, { rows, series: PROGRESS_SERIES, unit: "%", yMax: 100 });

  document.getElementById("chart-title").textContent = selectedJob
    ? `Températures — ${selectedJob.file_name || "impression"} (${formatDate(selectedJob.started_at)})`
    : "Températures";
  document.getElementById("chart-reset").hidden = !selectedJob;
}

// ── Tableau des impressions ──
function filamentCell(list) {
  if (!list || list.length === 0) return "—";
  return list.map(f => {
    const hex = /^[0-9A-Fa-f]{6}/.test(f.color || "") ? f.color.slice(0, 6) : "4E5867";
    return `<span class="filament-chip"><span class="legend-swatch" style="background:#${hex}"></span>` +
           `${escapeHtml(f.type || "?")} ${f.used_pct} %</span>`;
  }).join(" ");
}

function jobRow(job) {
  const progress = job.last_percent != null ? `${job.last_percent} %` : "—";
  const layers = job.last_layer != null && job.total_layers ? ` · couche ${job.last_layer}/${job.total_layers}` : "";
  return `<tr data-job="${job.id}" class="${selectedJob && selectedJob.id === job.id ? "selected" : ""}">
    <td>${formatDate(job.started_at)}</td>
    <td class="file-cell" title="${escapeHtml(job.file_name)}">${escapeHtml(job.file_name || "—")}</td>
    <td>${formatDuration(job.duration_s)}</td>
    <td><span class="job-status ${job.status}">${STATUS_ICONS[job.status] || ""} ${escapeHtml(job.status_label)}</span></td>
    <td>${progress}${layers}</td>
    <td>${filamentCell(job.filament)}</td>
    <td>${escapeHtml(job.error_code || "—")}</td>
    <td class="ai-cell">${job.photo ? `<a href="/api/ai/photo/${encodeURIComponent(job.photo)}" target="_blank" rel="noopener" title="Photo de fin">📷</a>` : ""}
      ${job.ai_comment ? `<span title="${escapeHtml(job.ai_comment)}">${escapeHtml(job.ai_comment.slice(0, 60))}${job.ai_comment.length > 60 ? "…" : ""}</span>` : ""}</td>
  </tr>`;
}

let jobsById = {};

async function loadJobs() {
  const { printer } = filters();
  const jobs = await getJSON(`/api/history/jobs?printer=${printer}&limit=100`);
  jobsById = Object.fromEntries(jobs.map(j => [j.id, j]));
  const body = document.querySelector("#jobs-table tbody");
  body.innerHTML = jobs.length
    ? jobs.map(jobRow).join("")
    : `<tr><td colspan="8" class="empty-cell">Aucune impression enregistrée pour l'instant.</td></tr>`;
}

async function loadErrors() {
  const { printer } = filters();
  const errors = await getJSON(`/api/history/errors?printer=${printer}&limit=50`);
  const body = document.querySelector("#errors-table tbody");
  body.innerHTML = errors.length
    ? errors.map(e => `<tr><td>${formatDate(e.timestamp)}</td><td class="mono">${escapeHtml(e.code)}</td>
        <td>${escapeHtml(PRINTER_STATE_LABELS[e.gcode_state] || e.gcode_state || "—")}</td><td>${escapeHtml(e.file_name || "—")}</td>
        <td><button type="button" class="btn btn-small" data-explain="${escapeHtml(e.code)}">Expliquer</button></td></tr>`).join("")
    : `<tr><td colspan="5" class="empty-cell">Aucune erreur enregistrée. 👍</td></tr>`;
}

async function refreshAll() {
  const results = await Promise.allSettled([loadStats(), loadCharts(), loadJobs(), loadErrors()]);
  const failed = results.find(r => r.status === "rejected");
  if (failed) console.warn("Historique :", failed.reason);
}

async function aiSummary() {
  const { printer, hours } = filters();
  const box = document.getElementById("ai-summary");
  const text = document.getElementById("ai-summary-text");
  box.hidden = false;
  text.textContent = "L'IA analyse l'historique… (peut prendre un moment)";
  const button = document.getElementById("ai-summary-btn");
  button.disabled = true;
  try {
    const { ok, json } = await BC.aiTask("/api/ai/summary",
      { printer, days: Math.max(1, Math.round(hours / 24)) },
      seconds => { text.textContent = `L'IA analyse l'historique… ${seconds} s`; });
    text.textContent = ok ? json.summary : (json.error || "Analyse impossible.");
  } catch (e) {
    text.textContent = `Bambu Control ne répond plus (${e.message}).`;
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ai-summary-btn").addEventListener("click", aiSummary);
  document.getElementById("history-clear").addEventListener("click", clearHistory);
  document.getElementById("ai-summary-close").addEventListener("click", () => { document.getElementById("ai-summary").hidden = true; });
  document.querySelector("#errors-table tbody").addEventListener("click", e => {
    const btn = e.target.closest("[data-explain]");
    if (btn) Assistant.explainError(btn.dataset.explain);
  });
  document.getElementById("filter-printer").addEventListener("change", () => { selectedJob = null; refreshAll(); });
  document.getElementById("filter-period").addEventListener("change", () => { selectedJob = null; refreshAll(); });
  document.getElementById("chart-reset").addEventListener("click", () => { selectedJob = null; loadCharts(); loadJobs(); });
  document.querySelector("#jobs-table tbody").addEventListener("click", e => {
    const row = e.target.closest("tr[data-job]");
    if (!row || filters().printer === "all") return;
    selectedJob = jobsById[row.dataset.job];
    loadCharts();
    loadJobs();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(loadCharts, 200); });
  refreshAll();
  setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS);
});
