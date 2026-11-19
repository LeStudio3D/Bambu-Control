// ═══════════════════════════════════════════════════════════
// Bambu Control — prints.js (page Impressions)
// Envoi de fichier avec vraie progression (XMLHttpRequest : fetch ne
// sait pas suivre l'envoi) et liste des dernières impressions.
// ═══════════════════════════════════════════════════════════

const ACCEPTED = [".gcode.3mf", ".3mf", ".gcode"];
const STATUS_ICONS = { finished: "✓", failed: "✕", stopped: "■", interrupted: "!", running: "●" };
let uploading = false;
let printerState = { online: false, state: null };

function setProgress(percent, label) {
  document.getElementById("upload-progress").hidden = false;
  document.getElementById("upload-bar").style.width = `${percent}%`;
  document.getElementById("upload-percent").textContent = label ?? `${percent} %`;
}

function uploadErrorMessage(xhr) {
  if (xhr.status === 0) return "Connexion au serveur perdue pendant l'envoi.";
  if (xhr.status === 413) return "Fichier trop gros.";
  try { return JSON.parse(xhr.responseText).error || `Erreur ${xhr.status}`; }
  catch (e) { return `Erreur ${xhr.status}`; }
}

function upload(file) {
  if (uploading) return;
  const name = file.name.toLowerCase();
  if (!ACCEPTED.some(ext => name.endsWith(ext))) {
    BC.toast("err", "Format refusé. Fichiers acceptés : .gcode.3mf, .3mf, .gcode.");
    return;
  }
  const startPrint = document.getElementById("start-print").checked;
  const form = new FormData();
  form.append("file", file);

  uploading = true;
  document.getElementById("upload-name").textContent = file.name;
  setProgress(0);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("X-CSRF-Token", BC.CSRF_TOKEN);
  // 1re étape : du navigateur vers Bambu Control (progression réelle)
  xhr.upload.addEventListener("progress", e => {
    if (e.lengthComputable) setProgress(Math.round((e.loaded / e.total) * 100));
  });
  // 2e étape : Bambu Control → carte SD de l'imprimante (pas de progression fournie)
  xhr.upload.addEventListener("load", () => {
    setProgress(100, "Envoi à l'imprimante…");
  });
  xhr.addEventListener("loadend", () => {
    uploading = false;
    if (xhr.status === 200) {
      const json = JSON.parse(xhr.responseText);
      BC.toast("ok", json.message);
      setProgress(100, "Terminé");
      loadRecentJobs();
      loadSdFiles();
      // Lancement demandé : on ouvre la fenêtre d'options (AMS, nivellement, timelapse…)
      if (startPrint) launchFile({ name: json.filename, size: file.size });
    } else {
      BC.toast("err", uploadErrorMessage(xhr));
      setProgress(0, "Échec");
    }
    document.getElementById("file-input").value = "";
  });
  xhr.send(form);
}

function setupUpload() {
  const zone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  ["dragenter", "dragover"].forEach(evt => zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach(evt => zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", e => { if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });
  input.addEventListener("change", () => { if (input.files[0]) upload(input.files[0]); });
}

function formatDate(iso) {
  const d = new Date(iso);
  return `${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
}

async function loadRecentJobs() {
  const box = document.getElementById("recent-jobs");
  try {
    const { json: jobs } = await BC.api("/api/history/jobs?limit=8");
    if (!jobs.length) {
      box.innerHTML = `<div class="muted">Aucune impression enregistrée pour l'instant.</div>`;
      return;
    }
    box.innerHTML = jobs.map(job => `
      <div class="list-item">
        <div style="min-width: 0">
          <div class="name" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap">${BC.escapeHtml(job.file_name || "—")}</div>
          <div class="meta">${formatDate(job.started_at)} · ${job.duration_s != null ? BC.formatMinutes(job.duration_s / 60) : "en cours"}</div>
        </div>
        <span class="job-status ${job.status}">${STATUS_ICONS[job.status] || ""} ${BC.escapeHtml(job.status_label)}</span>
      </div>`).join("");
  } catch (e) {
    box.innerHTML = `<div class="muted">Historique indisponible.</div>`;
  }
}

// ── Fichiers de la carte SD ──
function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} Mo`;
  return `${Math.max(1, Math.round(bytes / 1024))} Ko`;
}

let sdFiles = [];

function renderSdFiles() {
  const box = document.getElementById("sd-files");
  if (!sdFiles.length) {
    box.innerHTML = `<div class="muted">Aucun fichier imprimable sur la carte SD.</div>`;
    return;
  }
  const canLaunch = printerState.online && !BC.ACTIVE_STATES.includes(printerState.state);
  box.innerHTML = sdFiles.map((f, i) => `
    <div class="list-item">
      <div style="min-width: 0">
        <div class="name" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap" title="${BC.escapeHtml(f.name)}">${BC.escapeHtml(f.name)}</div>
        <div class="meta">${formatSize(f.size)}${f.modified ? " · " + formatDate(f.modified) : ""}</div>
      </div>
      <div class="list-actions">
        ${f.name.toLowerCase().endsWith(".3mf") ? `<button type="button" class="btn btn-small" data-info="${i}">Infos</button>` : ""}
        <button type="button" class="btn btn-small" data-queue="${i}" title="Ajouter à la file d'impression">File</button>
        <button type="button" class="btn btn-small btn-primary" data-launch="${i}" ${canLaunch ? "" : "disabled"}
          title="${canLaunch ? "" : "Impossible pendant une impression ou hors ligne"}">Lancer</button>
        <button type="button" class="btn btn-small btn-danger" data-delete="${i}">Supprimer</button>
      </div>
    </div>
    <div class="file-info" data-info-box="${i}" hidden></div>`).join("");
}

async function loadSdFiles() {
  const box = document.getElementById("sd-files");
  box.innerHTML = `<div class="muted">Lecture de la carte SD…</div>`;
  try {
    const { ok, json } = await BC.api("/api/files");
    if (!ok) {
      sdFiles = [];
      box.innerHTML = `<div class="muted">${BC.escapeHtml(json.error || "Carte SD inaccessible.")}</div>`;
      return;
    }
    sdFiles = json.files;
    renderSdFiles();
  } catch (e) {
    box.innerHTML = `<div class="muted">Bambu Control ne répond pas (le serveur est-il lancé ?).</div>`;
  }
}

// ── Fenêtre de lancement : plateau, AMS ou bobine externe, options ──
const launch = { info: null, source: "ams" };

function amsSlots() {
  return (BC.currentStatus()?.data?.ams || []).filter(s => s.global_index >= 0);
}

function slotOption(slot, selected) {
  const what = slot.empty ? "vide" : `${slot.type || "?"}${slot.remaining != null ? " · " + slot.remaining + " %" : ""}`;
  return `<option value="${slot.global_index}" ${slot.global_index === selected ? "selected" : ""}>${BC.escapeHtml(slot.label)} — ${BC.escapeHtml(what)}</option>`;
}

function currentPlate() {
  const index = Number(document.getElementById("launch-plate").value) || 1;
  return launch.info?.plates.find(p => p.index === index) || null;
}

function renderMapping() {
  const box = document.getElementById("launch-mapping");
  const plate = currentPlate();
  const slots = amsSlots();
  if (launch.source !== "ams") {
    const ext = BC.currentStatus()?.data?.external_spool;
    box.innerHTML = `<div class="muted">L'imprimante utilisera la bobine du support externe${ext && ext.type ? ` (${BC.escapeHtml(ext.type)})` : ""}.</div>`;
    return;
  }
  if (!slots.length) {
    box.innerHTML = `<div class="muted">Aucun AMS détecté : choisis « Bobine externe ».</div>`;
    return;
  }
  if (!plate || !plate.filaments.length) {
    box.innerHTML = `<div class="muted">Filaments non indiqués dans le fichier : le filament 1 utilisera l'emplacement choisi.</div>
      <div class="map-row"><div class="map-file">Filament 1</div>
        <select class="input" data-map-id="1">${slots.map(s => slotOption(s, slots[0].global_index)).join("")}</select></div>`;
    return;
  }
  box.innerHTML = plate.suggestions.map(sug => {
    const f = sug.filament;
    const hex = BC.hexColor((f.color || "").replace("#", ""));
    const chosen = sug.tray ?? slots.find(s => !s.empty)?.global_index ?? slots[0].global_index;
    return `<div class="map-row">
      <div class="map-file"><span class="legend-swatch" style="background:#${hex}"></span>
        <span>${BC.escapeHtml(f.type || "?")} <small>filament ${BC.escapeHtml(f.id)}${f.used_g != null ? " · " + formatGrams(f.used_g) : ""}</small></span></div>
      <select class="input" data-map-id="${BC.escapeHtml(f.id)}">${slots.map(s => slotOption(s, chosen)).join("")}</select>
      ${sug.tray == null ? `<div class="map-note">${BC.escapeHtml(sug.note)} : vérifie la bobine choisie.</div>` : ""}
    </div>`;
  }).join("");
}

function renderPlateOptions() {
  const plate = currentPlate();
  const timelapse = document.getElementById("launch-timelapse");
  const byObject = !!plate?.by_object;
  timelapse.disabled = byObject;
  if (byObject) timelapse.checked = false;
  document.getElementById("launch-timelapse-note").textContent =
    byObject ? "— impossible : ce plateau est imprimé « par objet »" : "";
  renderMapping();
}

function setSource(source) {
  launch.source = source;
  document.querySelectorAll("#launch-source [data-source]").forEach(b =>
    b.classList.toggle("active", b.dataset.source === source));
  renderMapping();
}

async function loadLaunchInfo(file) {
  const is3mf = file.name.toLowerCase().endsWith(".3mf");
  document.getElementById("launch-3mf-options").hidden = !is3mf;
  const warning = document.getElementById("launch-model-warning");
  warning.hidden = true;
  launch.info = null;
  if (!is3mf) return;
  const loading = document.getElementById("launch-loading");
  const plateSelect = document.getElementById("launch-plate");
  loading.hidden = false;
  loading.textContent = "Lecture du fichier sur la carte SD…";
  plateSelect.innerHTML = `<option value="1">Plateau 1</option>`;
  setSource(amsSlots().length ? "ams" : "external");
  renderPlateOptions();
  try {
    const { ok, json } = await BC.api(`/api/files/info?name=${encodeURIComponent(file.name)}&size=${file.size ?? ""}`);
    if (!ok) throw new Error(json.error || "Infos indisponibles");
    launch.info = json;
    loading.hidden = true;
    plateSelect.innerHTML = json.plates.map(p =>
      `<option value="${p.index}">Plateau ${p.index}${p.prediction_s ? " · " + BC.formatMinutes(p.prediction_s / 60) : ""}</option>`).join("");
    const printerModel = document.getElementById("launch-dialog").dataset.printerModel;
    if (json.printer_model && printerModel && json.printer_model !== printerModel) {
      warning.textContent = `Fichier tranché pour « ${json.printer_model} », imprimante « ${printerModel} » : l'impression risque d'échouer.`;
      warning.hidden = false;
    }
  } catch (e) {
    loading.textContent = `Infos du fichier indisponibles (${e.message}) : réglages par défaut.`;
  }
  renderPlateOptions();
}

function launchOptions() {
  const plate = currentPlate();
  const options = {
    plate: Number(document.getElementById("launch-plate").value) || 1,
    use_ams: launch.source === "ams",
    bed_leveling: document.getElementById("launch-bed-leveling").checked,
    flow_cali: document.getElementById("launch-flow-cali").checked,
    timelapse: document.getElementById("launch-timelapse").checked,
    by_object: !!plate?.by_object,
  };
  if (options.use_ams) {
    // Un emplacement par filament du projet (index = n° de filament - 1), -1 = non utilisé
    const selects = [...document.querySelectorAll("#launch-mapping [data-map-id]")];
    const maxId = Math.max(launch.info?.filament_count || 0, ...selects.map(s => Number(s.dataset.mapId) || 1));
    const mapping = Array(maxId).fill(-1);
    selects.forEach(s => { mapping[(Number(s.dataset.mapId) || 1) - 1] = Number(s.value); });
    options.ams_mapping = mapping;
  }
  return options;
}

async function askLaunchOptions(file, title = "Lancer l'impression ?", okLabel = "Lancer") {
  const dialog = document.getElementById("launch-dialog");
  document.getElementById("launch-title").textContent = title;
  document.getElementById("launch-ok").textContent = okLabel;
  document.getElementById("launch-name").textContent = file.name;
  const closed = new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
  dialog.returnValue = "";
  dialog.showModal();
  await loadLaunchInfo(file);
  return (await closed) ? launchOptions() : null;
}

async function launchFile(file) {
  const options = await askLaunchOptions(file);
  if (!options) return;
  const params = { filename: file.name, ...options };
  // 1er essai : le serveur fait vérifier le plateau par l'IA (si activé)
  BC.toast("pending", "Vérification puis lancement, attente de la confirmation de l'imprimante…", { sticky: true });
  const { json } = await BC.api("/api/command/start", { method: "POST", body: params });
  if (json.plate_check) {
    const force = await BC.confirmDialog({
      title: "Le plateau ne semble pas vide",
      message: `${json.plate_check.description} Lancer quand même ?`,
      confirmLabel: "Lancer quand même",
    });
    if (!force) { BC.toast("warn", "Lancement annulé."); return; }
    const status = await BC.sendCommand("start", { ...params, force: true });
    if (status === "confirmed") loadRecentJobs();
    return;
  }
  const kind = { confirmed: "ok", unconfirmed: "warn" }[json.status] || "err";
  BC.toast(kind, json.message || json.error || "Erreur inconnue");
  if (json.status === "confirmed") loadRecentJobs();
  BC.refreshStatus();
}

function setupLaunchDialog() {
  document.getElementById("launch-plate").addEventListener("change", renderPlateOptions);
  document.querySelectorAll("#launch-source [data-source]").forEach(b =>
    b.addEventListener("click", () => setSource(b.dataset.source)));
}

// ── Infos d'un fichier .3mf (vignette, durée, poids, bobine conseillée) ──
function formatGrams(g) { return g == null ? "—" : `${String(g).replace(".", ",")} g`; }

function plateHtml(plate, key) {
  const filaments = plate.suggestions.map(s => {
    const f = s.filament;
    const hex = BC.hexColor((f.color || "").replace("#", ""));
    return `<div><span class="legend-swatch" style="background:#${hex}"></span> ${BC.escapeHtml(f.type || "?")}
      ${f.used_g != null ? "· " + formatGrams(f.used_g) : ""}
      <span class="suggest">→ ${s.slot ? `bobine ${BC.escapeHtml(s.slot)} (${BC.escapeHtml(s.note)})` : BC.escapeHtml(s.note)}</span></div>`;
  }).join("") || `<div class="muted">Filaments non indiqués dans le fichier.</div>`;
  return `<div class="plate">
    ${plate.thumbnail_url ? `<img src="${plate.thumbnail_url}" alt="Vignette du plateau ${plate.index}">` : `<div></div>`}
    <div class="plate-facts">
      <strong>Plateau ${plate.index}</strong>
      <div>Durée estimée : ${plate.prediction_s ? BC.formatMinutes(plate.prediction_s / 60) : "—"} · Poids : ${formatGrams(plate.weight_g)}</div>
      ${filaments}
      ${plate.thumbnail_url ? `<div><button type="button" class="link-button" data-describe="${key}:${plate.index}">Décrire avec l'IA →</button>
        <div class="muted" data-describe-result="${key}:${plate.index}"></div></div>` : ""}
    </div></div>`;
}

async function toggleInfo(index) {
  const box = document.querySelector(`[data-info-box="${index}"]`);
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="muted">Lecture du fichier sur la carte SD…</div>`;
  const file = sdFiles[index];
  const { ok, json } = await BC.api(`/api/files/info?name=${encodeURIComponent(file.name)}&size=${file.size}`);
  box.innerHTML = ok ? json.plates.map(p => plateHtml(p, json.key)).join("")
                     : `<div class="muted">${BC.escapeHtml(json.error || "Infos indisponibles.")}</div>`;
}

async function describePlate(ref) {
  const [key, plate] = ref.split(":");
  const out = document.querySelector(`[data-describe-result="${ref}"]`);
  out.textContent = "L'IA regarde la vignette…";
  try {
    const { ok, json } = await BC.aiTask("/api/files/describe", { key, plate: Number(plate) },
      seconds => { out.textContent = `L'IA regarde la vignette… ${seconds} s`; });
    out.textContent = ok ? json.description : (json.error || "Description impossible.");
  } catch (e) {
    out.textContent = `Bambu Control ne répond plus (${e.message}).`;
  }
}

// ── File d'impression ──
async function loadQueue() {
  const box = document.getElementById("queue");
  try {
    const { json } = await BC.api("/api/queue");
    const items = json.items || [];
    box.innerHTML = items.length ? items.map((item, i) => `
      <div class="queue-item">
        <span class="pos">${i + 1}</span>
        <div style="min-width: 0"><div class="name" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap">${BC.escapeHtml(item.name)}</div>
          <div class="meta">plateau ${item.plate} · ${item.use_ams ? "AMS" : "bobine externe"}${item.timelapse ? " · timelapse" : ""}</div></div>
        <div class="list-actions">
          ${i > 0 ? `<button type="button" class="btn btn-small" data-queue-up="${i}" title="Monter">↑</button>` : ""}
          <button type="button" class="btn btn-small btn-danger" data-queue-remove="${i}">Retirer</button>
        </div>
      </div>`).join("")
      : `<div class="muted">La file est vide. Ajoute des fichiers avec le bouton « File » ci-dessus.</div>`;
  } catch (e) {
    box.innerHTML = `<div class="muted">File indisponible.</div>`;
  }
}

async function addToQueue(file) {
  const options = await askLaunchOptions(file, "Ajouter à la file d'impression ?", "Ajouter");
  if (!options) return;
  const { ok, json } = await BC.api("/api/queue/add", { method: "POST", body: { name: file.name, ...options } });
  BC.toast(ok ? "ok" : "err", json.message || json.error);
  loadQueue();
}

async function queueAction(action, index) {
  await BC.api(`/api/queue/${action}`, { method: "POST", body: { index } });
  loadQueue();
}

async function deleteFile(file) {
  const ok = await BC.confirmDialog({
    title: "Supprimer ce fichier ?",
    message: `« ${file.name} » sera effacé de la carte SD de l'imprimante. Cette action est définitive.`,
    confirmLabel: "Supprimer",
  });
  if (!ok) return;
  const { ok: done, json } = await BC.api("/api/files/delete", { method: "POST", body: { name: file.name } });
  BC.toast(done ? "ok" : "err", json.message || json.error || "Erreur inconnue");
  if (done) loadSdFiles();
}

function setupSdFiles() {
  document.getElementById("refresh-files").addEventListener("click", loadSdFiles);
  document.getElementById("sd-files").addEventListener("click", e => {
    const launch = e.target.closest("[data-launch]");
    const del = e.target.closest("[data-delete]");
    const info = e.target.closest("[data-info]");
    const queue = e.target.closest("[data-queue]");
    const describe = e.target.closest("[data-describe]");
    if (launch) launchFile(sdFiles[launch.dataset.launch]);
    if (del) deleteFile(sdFiles[del.dataset.delete]);
    if (info) toggleInfo(Number(info.dataset.info));
    if (queue) addToQueue(sdFiles[queue.dataset.queue]);
    if (describe) describePlate(describe.dataset.describe);
  });
  document.getElementById("queue").addEventListener("click", e => {
    const up = e.target.closest("[data-queue-up]");
    const remove = e.target.closest("[data-queue-remove]");
    if (up) queueAction("up", Number(up.dataset.queueUp));
    if (remove) queueAction("remove", Number(remove.dataset.queueRemove));
  });
  loadQueue();
  BC.onStatus(status => {
    const next = { online: status.connection === "online", state: status.data?.gcode_state };
    const wasOnline = printerState.online;
    const changed = next.online !== printerState.online || next.state !== printerState.state;
    printerState = next;
    if (next.online && !wasOnline) loadSdFiles();      // la carte devient lisible
    else if (!next.online) {
      document.getElementById("sd-files").innerHTML =
        `<div class="muted">Imprimante hors ligne : la carte SD n'est pas accessible pour l'instant.</div>`;
    } else if (changed) renderSdFiles();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupUpload();
  setupLaunchDialog();
  setupSdFiles();
  loadRecentJobs();
  setInterval(() => { if (!document.hidden) loadRecentJobs(); }, 60000);
});
