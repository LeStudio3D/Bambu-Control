// ═══════════════════════════════════════════════════════════
// Bambu Control — printer.js (page Imprimante : réglages en direct)
// ═══════════════════════════════════════════════════════════

const SPEED_LEVEL_BY_PERCENT = { 50: 1, 100: 2, 124: 3, 166: 4 };
const CAMERA_LABELS = { live: "En direct", connecting: "Connexion", unavailable: "Indisponible", stopped: "En veille" };
let current = { data: {} };
const prefilled = {};       // consignes pré-remplies une seule fois (on n'écrase pas une saisie)
const sliderBusy = {};      // curseur en cours de manipulation

function render(status) {
  current = status;
  const data = status.data || {};
  const online = status.connection === "online";
  const busy = BC.isBusy();
  const printing = BC.ACTIVE_STATES.includes(data.gcode_state);

  document.getElementById("dev-mode-warning").hidden = data.command_health !== "ignored";

  // Lumière
  const lightOn = data.light_state === "on";
  document.getElementById("light-state").textContent = data.light_state ? (lightOn ? "Allumée" : "Éteinte") : "État inconnu";
  const light = document.getElementById("btn-light");
  light.classList.toggle("is-on", lightOn);
  light.disabled = !online || busy || !data.light_state;
  document.getElementById("btn-light-label").textContent = lightOn ? "Éteindre" : "Allumer";

  // Vitesse
  const activeLevel = SPEED_LEVEL_BY_PERCENT[data.print_speed];
  document.querySelectorAll("#speed [data-level]").forEach(btn => {
    btn.classList.toggle("active", printing && Number(btn.dataset.level) === activeLevel);
    btn.disabled = !online || busy || !printing;
  });

  // Consignes
  [["nozzle_temp", data.nozzle_temp, data.nozzle_target, "nozzle-now"],
   ["bed_temp", data.bed_temp, data.bed_target, "bed-now"]].forEach(([part, now, target, nowId]) => {
    document.getElementById(nowId).textContent = now == null ? "actuelle —" : `actuelle ${Math.round(now)} °C`;
    const box = document.querySelector(`.stepper[data-part="${part}"]`);
    if (!prefilled[part] && online && target != null) {
      box.querySelector("input").value = Math.round(target);
      prefilled[part] = true;
    }
    box.querySelectorAll("button").forEach(b => { b.disabled = !online || busy; });
  });

  // Ventilateurs (seulement ceux que le modèle possède)
  const fans = data.fans_available || ["part"];
  document.querySelectorAll("[data-fan]").forEach(field => {
    const fan = field.dataset.fan;
    field.hidden = !fans.includes(fan);
    const input = field.querySelector("input");
    input.disabled = !online || busy;
    const pct = data[`fan_${fan}_pct`];
    if (!sliderBusy[fan] && pct != null) {
      input.value = pct;
      field.querySelector("output").textContent = `${pct} %`;
    }
  });

  // Informations
  document.getElementById("info-state").textContent = online ? (BC.STATE_LABELS[data.gcode_state] || "—") : "Hors ligne";
  document.getElementById("info-wifi").textContent = data.wifi_signal || "—";
  document.getElementById("info-chamber").textContent = data.has_chamber_sensor
    ? (data.chamber_temp == null ? "—" : `${Math.round(data.chamber_temp)} °C`) : "Pas de capteur";
  document.getElementById("info-camera").textContent = CAMERA_LABELS[data.camera?.state] || "—";

  renderDrying(data, online);
}

// ── AMS et séchage ──
// Valeurs du guide de séchage Bambu Lab ([temp °C, heures]) ; null = déconseillé sur ce modèle
const DRY_PRESETS = {
  "AMS 2 Pro": { PLA: [45, 12], "PLA-CF": [45, 12], PETG: [65, 12], "PETG-CF": [65, 12], TPU: [65, 12], SUPPORT: [60, 12] },
  "AMS HT": { PLA: [45, 12], "PLA-CF": [45, 12], PETG: [65, 12], "PETG-CF": [65, 12], TPU: [75, 18], SUPPORT: [60, 12],
              ABS: [80, 8], ASA: [80, 8], PC: [80, 8], "PET-CF": [80, 12], PA: [85, 12], "PA-CF": [85, 12],
              "PAHT-CF": [85, 12], PVA: [85, 18] },
};
const DRY_FILAMENTS = ["PLA", "PLA-CF", "PETG", "PETG-CF", "TPU", "ABS", "ASA", "PC", "PA", "PA-CF", "PAHT-CF",
                       "PET-CF", "PVA", "SUPPORT"];
let dryLayout = "";   // on ne reconstruit les formulaires que si la liste des AMS change (garde la saisie)

function dryFormHtml(unit) {
  const loaded = unit.slots.find(s => !s.empty && s.type);
  const guess = loaded ? DRY_FILAMENTS.find(f => loaded.type.toUpperCase().startsWith(f)) || "PLA" : "PLA";
  const options = DRY_FILAMENTS.map(f =>
    `<option value="${f}" ${f === guess ? "selected" : ""}>${f}${DRY_PRESETS[unit.model][f] ? "" : " (déconseillé)"}</option>`).join("");
  return `<form class="dry-form" data-dry-form="${unit.id}">
    <div class="field"><label>Filament</label><select class="input" name="filament">${options}</select></div>
    <div class="field"><label>Température (°C)</label>
      <input class="input" type="number" name="temp" min="45" max="${unit.dry_max_temp}" step="5" inputmode="numeric"></div>
    <div class="field"><label>Durée (heures)</label>
      <input class="input" type="number" name="hours" min="1" max="24" step="1" inputmode="numeric"></div>
    <label class="check"><input type="checkbox" name="rotate"> Faire tourner les bobines pendant le séchage
      <span class="muted">(séchage plus homogène)</span></label>
    <div class="dry-actions">
      <button type="submit" class="btn btn-primary" data-dry-start>Lancer le séchage</button>
      <button type="button" class="btn btn-danger" data-dry-stop>Arrêter le séchage</button>
    </div>
  </form>`;
}

function applyPreset(form, model) {
  const preset = DRY_PRESETS[model][form.filament.value];
  if (preset) {
    form.temp.value = preset[0];
    form.hours.value = preset[1];
  }
}

function renderDrying(data, online) {
  const box = document.getElementById("dry-units");
  const units = data.ams_units || [];
  const layout = units.map(u => `${u.id}:${u.model}:${u.remote_dry}`).join(",");
  if (layout !== dryLayout) {
    dryLayout = layout;
    box.innerHTML = units.length ? units.map(unit => `
      <div class="dry-unit" data-dry-unit="${unit.id}">
        <div class="ams-unit-head"><strong>${BC.escapeHtml(unit.name)}</strong>
          <span class="muted">${BC.escapeHtml(unit.model)}</span><span class="ams-facts" data-facts></span></div>
        <div data-slots></div>
        ${!unit.can_dry
          ? `<p class="hint" style="margin-top: 8px">Pas de séchage sur ce modèle (AMS 2 Pro ou AMS HT seulement).</p>`
          : !unit.remote_dry
          ? `<p class="hint" style="margin-top: 8px">Le firmware de ${BC.escapeHtml(data.printer_name || "cette imprimante")}
              ne permet pas de lancer le séchage à distance (Bambu Studio non plus) : lance-le depuis l'écran de
              l'imprimante. Bambu Control affichera le séchage en cours et le temps restant.</p>`
          : dryFormHtml(unit) + `<p class="hint" data-dry-blocked hidden></p>`}
      </div>`).join("")
      : `<div class="ams-empty-msg">Aucun AMS détecté.</div>`;
    box.querySelectorAll("[data-dry-form]").forEach(form => {
      const unit = units.find(u => String(u.id) === form.dataset.dryForm);
      applyPreset(form, unit.model);
      form.filament.addEventListener("change", () => applyPreset(form, unit.model));
      form.addEventListener("submit", e => { e.preventDefault(); startDrying(form, unit); });
      form.querySelector("[data-dry-stop]").addEventListener("click", () => stopDrying(unit));
    });
  }
  // Mise à jour en direct (sans toucher à la saisie)
  const busy = BC.isBusy();
  units.forEach(unit => {
    const el = box.querySelector(`[data-dry-unit="${unit.id}"]`);
    if (!el) return;
    el.querySelector("[data-facts]").innerHTML = BC.amsUnitFacts(unit);
    el.querySelector("[data-slots]").innerHTML = BC.amsSlotsHtml(unit.slots);
    const form = el.querySelector("[data-dry-form]");
    if (form) {
      const blocked = (unit.dry_blocked || []).length > 0;
      form.querySelector("[data-dry-start]").disabled = !online || busy || unit.drying || blocked;
      form.querySelector("[data-dry-stop]").disabled = !online || busy || !unit.drying;
      const note = el.querySelector("[data-dry-blocked]");
      note.hidden = !blocked;
      note.textContent = blocked ? `Séchage impossible pour l'instant : ${unit.dry_blocked.join(", ")}.` : "";
    }
  });
}

function startDrying(form, unit) {
  const temp = parseInt(form.temp.value, 10);
  const hours = parseInt(form.hours.value, 10);
  if (Number.isNaN(temp) || temp < 45 || temp > unit.dry_max_temp) {
    BC.toast("err", `Température : entre 45 et ${unit.dry_max_temp} °C pour un ${unit.model}.`);
    return;
  }
  if (Number.isNaN(hours) || hours < 1 || hours > 24) {
    BC.toast("err", "Durée : entre 1 et 24 heures.");
    return;
  }
  const params = { ams_id: unit.id, filament: form.filament.value, temp, hours, rotate: form.rotate.checked };
  BC.sendCommand("dry", params, {
    title: `Sécher ${unit.name} ?`,
    message: `${params.filament} à ${temp} °C pendant ${hours} h` +
      `${params.rotate ? ", bobines en rotation" : ", bobines immobiles"}.` +
      (DRY_PRESETS[unit.model][params.filament] ? "" : ` Attention : ${params.filament} est déconseillé dans un ${unit.model}.`),
    confirmLabel: "Lancer le séchage",
  });
}

function stopDrying(unit) {
  BC.sendCommand("dry_stop", { ams_id: unit.id }, {
    title: `Arrêter le séchage de ${unit.name} ?`,
    message: "Le chauffage de l'AMS sera coupé.",
    confirmLabel: "Arrêter",
  });
}

function setupSteppers() {
  document.querySelectorAll(".stepper").forEach(box => {
    const input = box.querySelector("input");
    const max = Number(box.dataset.max);
    box.querySelectorAll("[data-step]").forEach(btn => btn.addEventListener("click", () => {
      const value = (parseInt(input.value, 10) || 0) + Number(btn.dataset.step);
      input.value = Math.min(max, Math.max(0, value));
    }));
    box.querySelector("[data-apply]").addEventListener("click", () => {
      const value = parseInt(input.value, 10);
      if (Number.isNaN(value) || value < 0 || value > max) {
        BC.toast("err", `Valeur invalide : entre 0 et ${max} °C.`);
        return;
      }
      BC.sendCommand(box.dataset.part, { value });
    });
  });
}

function setupFans() {
  document.querySelectorAll("[data-fan]").forEach(field => {
    const fan = field.dataset.fan;
    const input = field.querySelector("input");
    input.addEventListener("input", () => {
      sliderBusy[fan] = true;
      field.querySelector("output").textContent = `${input.value} %`;
    });
    input.addEventListener("change", async () => {
      await BC.sendCommand("fan", { fan, percent: parseInt(input.value, 10) });
      sliderBusy[fan] = false;
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  BC.onStatus(render);
  BC.onBusy(() => render(current));
  setupSteppers();
  setupFans();
  document.getElementById("btn-light").addEventListener("click", () =>
    BC.sendCommand("light", { on: current.data?.light_state !== "on" }));
  document.querySelectorAll("#speed [data-level]").forEach(btn =>
    btn.addEventListener("click", () => BC.sendCommand("speed", { level: Number(btn.dataset.level) })));
});
