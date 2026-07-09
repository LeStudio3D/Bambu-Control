// ═══════════════════════════════════════════════════════════
// Bambu Control — dashboard.js
// ═══════════════════════════════════════════════════════════

const STATUS_POLL_MS = 2000;
const CAMERA_POLL_MS = 600;

const GAUGES_BASE = [
  { key: "nozzle_temp", label: "BUSE", max: 320, hot: true, targetKey: "nozzle_target" },
  { key: "bed_temp", label: "PLATEAU", max: 120, hot: true, targetKey: "bed_target" },
];
const CHAMBER_GAUGE = { key: "chamber_temp", label: "CHAMBRE", max: 60, hot: false, targetKey: null };

let currentGauges = [...GAUGES_BASE]; // ajusté dynamiquement selon has_chamber_sensor
let chamberGaugeKnown = false;

const STATE_LABELS = {
  "GcodeState.IDLE": "IDLE",
  "GcodeState.RUNNING": "IMPRESSION",
  "GcodeState.PAUSE": "EN PAUSE",
  "GcodeState.FINISH": "TERMINÉ",
  "GcodeState.FAILED": "ÉCHEC",
  "GcodeState.PREPARE": "PRÉPARATION",
  "None": "INCONNU",
};

function buildGaugeSVG(id, pct) {
  // Arc de 270° façon jauge de cockpit
  const r = 26;
  const circumference = 2 * Math.PI * r * 0.75; // 270°
  const offset = circumference * (1 - pct);
  return `
    <svg width="72" height="72" viewBox="0 0 72 72">
      <circle class="gauge-arc-bg" cx="36" cy="36" r="${r}"
        stroke-dasharray="${circumference} 999"
        transform="rotate(135 36 36)" />
      <circle class="gauge-arc-fg ${id.hot ? 'hot' : ''}" cx="36" cy="36" r="${r}"
        stroke-dasharray="${circumference - offset} 999"
        transform="rotate(135 36 36)" />
    </svg>`;
}

function renderGauges() {
  const row = document.getElementById("gauges-row");
  row.style.gridTemplateColumns = `repeat(${currentGauges.length}, 1fr)`;
  row.innerHTML = currentGauges.map(g => `
    <div class="gauge" id="gauge-${g.key}">
      ${buildGaugeSVG(g, 0)}
      <div class="gauge-value" id="gauge-value-${g.key}">--<span>°C</span></div>
      <div class="gauge-label">${g.label}</div>
    </div>
  `).join("");
}

function initGauges() {
  renderGauges();
}

function ensureChamberGaugeState(hasChamberSensor) {
  if (chamberGaugeKnown) return;
  chamberGaugeKnown = true;
  if (hasChamberSensor) {
    currentGauges = [...GAUGES_BASE, CHAMBER_GAUGE];
    renderGauges();
  }
  // si pas de capteur chambre, on garde juste les 2 jauges de base (déjà rendu)
}

function updateGauges(data) {
  currentGauges.forEach(g => {
    const val = data[g.key];
    const container = document.getElementById(`gauge-${g.key}`);
    if (!container) return;
    if (val === null || val === undefined) {
      const valueEl = document.getElementById(`gauge-value-${g.key}`);
      if (valueEl) valueEl.innerHTML = `--<span>°C</span>`;
      return;
    }
    const pct = Math.min(val / g.max, 1);
    const target = g.targetKey ? data[g.targetKey] : null;
    const targetHtml = (target !== null && target !== undefined && target > 0)
      ? `<div class="gauge-target">→ ${Math.round(target)}°C</div>` : "";
    container.innerHTML = buildGaugeSVG(g, pct) +
      `<div class="gauge-value" id="gauge-value-${g.key}">${Math.round(val)}<span>°C</span></div>
       <div class="gauge-label">${g.label}</div>
       ${targetHtml}`;
  });
}

function updateConnectionPill(connected) {
  const pill = document.getElementById("connection-pill");
  const label = document.getElementById("connection-label");
  if (connected) {
    pill.classList.remove("offline");
    pill.classList.add("online");
    label.textContent = "IMPRIMANTE CONNECTÉE";
  } else {
    pill.classList.remove("online");
    pill.classList.add("offline");
    label.textContent = "DÉCONNECTÉE";
  }
}

function updateAMS(slots) {
  const row = document.getElementById("ams-row");
  if (!slots || slots.length === 0) {
    row.innerHTML = `<div class="ams-empty">Pas de données AMS (vérifie que l'AMS est bien détecté par l'imprimante)</div>`;
    return;
  }
  row.innerHTML = slots.map(s => {
    if (s.empty) {
      return `
        <div class="ams-slot">
          <div class="ams-swatch" style="background:transparent; border-style:dashed;"></div>
          <div class="ams-percent">vide</div>
        </div>`;
    }
    const hex = (s.color || "4E5867").replace('#', '').slice(0, 6);
    const remaining = (s.remaining !== null && s.remaining !== undefined) ? `${s.remaining}%` : "?";
    return `
      <div class="ams-slot" title="${s.type || ''}">
        <div class="ams-swatch" style="background:#${hex};"></div>
        <div class="ams-percent">${remaining}</div>
      </div>`;
  }).join("");
}

function formatRemaining(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  if (minutes <= 0) return "0 min";
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return h > 0 ? `${h}h${m.toString().padStart(2, "0")}` : `${m} min`;
}

async function pollStatus() {
  try {
    const res = await fetch("/api/status");
    if (res.status === 401) { window.location.href = "/login"; return; }
    const json = await res.json();
    updateConnectionPill(json.connected);

    const data = json.data || {};

    // État / badge
    const badge = document.getElementById("state-badge");
    badge.textContent = STATE_LABELS[data.gcode_state] || data.gcode_state || "—";

    // Progression
    const pct = data.mc_percent || 0;
    document.getElementById("progress-percent").textContent = `${pct}%`;
    document.getElementById("progress-fill").style.width = `${pct}%`;
    document.getElementById("progress-remaining").textContent =
      `Reste ${formatRemaining(data.remaining_time_min)}`;

    // Fichier / couches
    document.getElementById("file-name").textContent = data.file_name || "Aucun fichier en cours";
    const cl = data.current_layer, tl = data.total_layers;
    document.getElementById("layer-info").textContent =
      (cl !== null && cl !== undefined && tl) ? `Couche ${cl}/${tl}` : "Couche —/—";

    // REC indicator
    const rec = document.getElementById("rec-indicator");
    const recLabel = document.getElementById("rec-label");
    const isPrinting = data.gcode_state === "GcodeState.RUNNING";
    rec.classList.toggle("idle", !isPrinting);
    recLabel.textContent = isPrinting ? "REC" : "IDLE";

    // Jauges (la jauge chambre n'apparaît que si le modèle en a une)
    ensureChamberGaugeState(!!data.has_chamber_sensor);
    updateGauges(data);

    // AMS
    updateAMS(data.ams);

    // Infos machine
    document.getElementById("info-speed").textContent =
      data.print_speed !== null && data.print_speed !== undefined ? `${data.print_speed}%` : "—";
    document.getElementById("info-wifi").textContent = data.wifi_signal || "—";
    const lightOn = (data.light_state || "").toLowerCase() === "on";
    document.getElementById("info-light").textContent = lightOn ? "Allumé" : "Éteint";

    // Synchronise le panneau de contrôle avec l'état réel
    updateControlPanel(data, lightOn);

  } catch (e) {
    updateConnectionPill(false);
  }
}

// ── Panneau de contrôle ───────────────────────────────────
let controlInitDone = false;
let fanDebounceTimers = {};

function updateControlPanel(data, lightOn) {
  // Toggle lumière
  const lightBtn = document.getElementById("light-toggle");
  lightBtn.textContent = lightOn ? "ALLUMÉE" : "ÉTEINTE";
  lightBtn.classList.toggle("on", lightOn);

  // Bouton de vitesse actif
  const speedMap = { 50: 1, 100: 2, 124: 3, 166: 4 };
  const activeLevel = speedMap[data.print_speed];
  document.querySelectorAll("#speed-buttons button").forEach(btn => {
    btn.classList.toggle("active", parseInt(btn.dataset.level) === activeLevel);
  });

  // Affiche/masque les contrôles liés à la chambre selon le modèle
  document.getElementById("chamber-control-block").style.display =
    data.has_chamber_sensor ? "block" : "none";
  document.getElementById("fan-chamber-block").style.display =
    data.has_chamber_sensor ? "block" : "none";

  // Pré-remplit les champs de consigne et les sliders une seule fois
  // (pour ne pas écraser ce que l'utilisateur est en train de taper/glisser)
  if (!controlInitDone) {
    if (data.nozzle_target !== null && data.nozzle_target !== undefined) {
      document.getElementById("nozzle-input").value = Math.round(data.nozzle_target);
    }
    if (data.bed_target !== null && data.bed_target !== undefined) {
      document.getElementById("bed-input").value = Math.round(data.bed_target);
    }
    setSliderIfIdle("fan-part", data.fan_part);
    setSliderIfIdle("fan-aux", data.fan_aux);
    setSliderIfIdle("fan-chamber", data.fan_chamber);
    controlInitDone = true;
  }
}

function setSliderIfIdle(prefix, rawValue) {
  if (rawValue === null || rawValue === undefined) return;
  const pct = Math.round((rawValue / 255) * 100);
  const slider = document.getElementById(`${prefix}-slider`);
  const label = document.getElementById(`${prefix}-value`);
  if (slider) slider.value = pct;
  if (label) label.textContent = `${pct}%`;
}

function showControlStatus(type, message) {
  const el = document.getElementById("control-status");
  el.className = `upload-status show ${type}`;
  el.textContent = message;
  setTimeout(() => { el.classList.remove("show"); }, 4000);
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || "Erreur inconnue");
  return json;
}

async function toggleLight() {
  const btn = document.getElementById("light-toggle");
  const turningOn = !btn.classList.contains("on");
  try {
    await postJSON("/api/control/light", { on: turningOn });
    showControlStatus("ok", `Commande envoyée (lumière ${turningOn ? "allumée" : "éteinte"} demandée). Vérifie sur l\'imprimante.`);
  } catch (e) {
    showControlStatus("err", `✗ ${e.message}`);
  }
}

async function setSpeed(level) {
  try {
    await postJSON("/api/control/speed", { level });
    showControlStatus("ok", "Commande envoyée. Vérifie sur l\'imprimante.");
  } catch (e) {
    showControlStatus("err", `✗ ${e.message}`);
  }
}

async function setTemperature(part) {
  const input = document.getElementById(`${part}-input`);
  const value = parseInt(input.value, 10);
  if (isNaN(value)) {
    showControlStatus("err", "Valeur de température invalide.");
    return;
  }
  try {
    await postJSON("/api/control/temperature", { part, value });
    showControlStatus("ok", `Commande envoyée (consigne ${part === "nozzle" ? "buse" : "plateau"} à ${value}°C). Vérifie sur l\'imprimante.`);
  } catch (e) {
    showControlStatus("err", `✗ ${e.message}`);
  }
}

function onFanSliderInput(fan, value) {
  document.getElementById(`fan-${fan}-value`).textContent = `${value}%`;
  clearTimeout(fanDebounceTimers[fan]);
  fanDebounceTimers[fan] = setTimeout(async () => {
    try {
      await postJSON("/api/control/fan", { fan, percent: parseFloat(value) });
      showControlStatus("ok", `Commande envoyée (ventilateur ${fan} à ${value}%). Vérifie sur l\'imprimante.`);
    } catch (e) {
      showControlStatus("err", `✗ ${e.message}`);
    }
  }, 400);
}

function pollCamera() {
  const img = document.getElementById("camera-img");
  const noFeed = document.getElementById("no-feed-msg");
  const testImg = new Image();
  testImg.onload = () => {
    img.src = testImg.src;
    img.style.display = "block";
    noFeed.style.display = "none";
  };
  testImg.onerror = () => {
    img.style.display = "none";
    noFeed.style.display = "flex";
  };
  testImg.src = `/api/camera.jpg?t=${Date.now()}`;
}

function updateClock() {
  document.getElementById("camera-clock").textContent =
    new Date().toLocaleTimeString("fr-FR");
}

// ── Upload ────────────────────────────────────────────────
function setupUpload() {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const statusEl = document.getElementById("upload-status");
  const startPrintCheckbox = document.getElementById("start-print-checkbox");

  dropzone.addEventListener("click", () => fileInput.click());

  ["dragover", "dragenter"].forEach(evt =>
    dropzone.addEventListener(evt, e => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach(evt =>
    dropzone.addEventListener(evt, e => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );

  dropzone.addEventListener("drop", e => {
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
  });

  async function uploadFile(file) {
    if (!file.name.toLowerCase().endsWith(".gcode")) {
      showStatus("err", `Extension refusée. Seuls les fichiers .gcode sont acceptés.`);
      return;
    }
    showStatus("pending", `Envoi de ${file.name} en cours…`);

    const formData = new FormData();
    formData.append("file", file);
    formData.append("start_print", startPrintCheckbox.checked ? "true" : "false");

    try {
      const res = await fetch("/api/upload", { method: "POST", body: formData });
      const json = await res.json();
      if (res.ok) {
        showStatus("ok", `✓ ${json.message}`);
      } else {
        showStatus("err", `✗ ${json.error || "Erreur inconnue"}`);
      }
    } catch (e) {
      showStatus("err", `✗ Échec de l'envoi : ${e}`);
    }
  }

  function showStatus(type, message) {
    statusEl.className = `upload-status show ${type}`;
    statusEl.textContent = message;
  }
}

// ── Historique (mini graphique canvas) ───────────────────────
async function loadHistoryChart() {
  try {
    const res = await fetch("/api/history?limit=100");
    const rows = await res.json();
    drawChart(rows);
  } catch (e) {
    // silencieux, pas critique
  }
}

function drawChart(rows) {
  const canvas = document.getElementById("history-chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (!rows || rows.length === 0) return;

  const maxTemp = 250;
  const series = [
    { field: "nozzle_temp", color: "#E3A857", scale: maxTemp },
    { field: "bed_temp", color: "#59C3C3", scale: maxTemp },
    { field: "mc_percent", color: "#6FCF97", scale: 100 },
  ];

  series.forEach(s => {
    ctx.beginPath();
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    rows.forEach((row, i) => {
      const val = row[s.field];
      if (val === null || val === undefined) return;
      const x = (i / (rows.length - 1)) * w;
      const y = h - (Math.min(val / s.scale, 1) * (h - 8)) - 4;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}

// ── Init ──────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initGauges();
  setupUpload();
  pollStatus();
  pollCamera();
  loadHistoryChart();
  updateClock();

  setInterval(pollStatus, STATUS_POLL_MS);
  setInterval(pollCamera, CAMERA_POLL_MS);
  setInterval(updateClock, 1000);
  setInterval(loadHistoryChart, 30000);
});
