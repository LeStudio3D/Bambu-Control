// ═══════════════════════════════════════════════════════════
// Bambu Control — filament.js (page Filament : stock des bobines)
// ═══════════════════════════════════════════════════════════

let stock = { spools: [], printers: [], slots: {} };
let editing = null;            // bobine en cours de modification (null = nouvelle)
const $ = id => document.getElementById(id);

function printerName(id) {
  return stock.printers.find(p => p.id === id)?.name || "?";
}

function grams(g) {
  return g >= 1000 ? `${(g / 1000).toFixed(2).replace(".", ",")} kg` : `${Math.round(g)} g`;
}

function renderSpools() {
  const box = $("spools");
  if (!stock.spools.length) {
    box.innerHTML = `<div class="muted">Aucune bobine pour l'instant. Ajoute celles que tu as en stock.</div>`;
    $("spool-summary").textContent = "";
    return;
  }
  const total = stock.spools.reduce((sum, s) => sum + s.remaining_g, 0);
  const low = stock.spools.filter(s => s.low).length;
  $("spool-summary").textContent = `${stock.spools.length} bobine(s) · ${grams(total)} au total` +
    (low ? ` · ${low} presque vide(s)` : "");
  box.innerHTML = stock.spools.map(s => {
    const where = s.printer_id ? `${BC.escapeHtml(printerName(s.printer_id))} · ${BC.escapeHtml(s.slot)}` : "En stock";
    const pct = Math.max(0, Math.min(100, s.percent ?? 0));
    return `<button type="button" class="spool ${s.low ? "is-low" : ""}" data-spool="${s.id}">
      <span class="spool-swatch" style="background:#${BC.hexColor(s.color, "4E5867")}"></span>
      <span class="spool-main">
        <strong>${BC.escapeHtml(s.name)}</strong>
        <span class="muted">${BC.escapeHtml(s.material)} · ${where}${s.price_kg ? ` · ${String(s.price_kg).replace(".", ",")} €/kg` : ""}</span>
        <span class="progress"><span class="progress-fill" style="width:${pct}%"></span></span>
      </span>
      <span class="spool-left"><strong>${grams(s.remaining_g)}</strong><span class="muted">${s.low ? "presque vide" : pct + " %"}</span></span>
    </button>`;
  }).join("");
}

async function loadSpools() {
  try {
    const { ok, json } = await BC.api("/api/spools");
    if (!ok) throw new Error(json.error || "Erreur");
    stock = json;
    renderSpools();
  } catch (e) {
    $("spools").innerHTML = `<div class="muted">Stock indisponible (${BC.escapeHtml(e.message)}).</div>`;
  }
}

// ── Formulaire ──
function fillSlots(printerId, selected) {
  const slots = stock.slots[printerId] || [];
  $("sp-slot").innerHTML = `<option value="">—</option>` + slots.map(s =>
    `<option value="${s}" ${s === selected ? "selected" : ""}>${s === "EXT" ? "Support externe" : s}</option>`).join("");
  $("sp-slot").disabled = !printerId;
}

function openEditor(spool = null) {
  editing = spool;
  const form = $("spool-form");
  $("spool-form-title").textContent = spool ? "Modifier la bobine" : "Nouvelle bobine";
  form.name.value = spool?.name || "";
  form.material.value = spool?.material || "PLA";
  form.color.value = `#${BC.hexColor(spool?.color, "FFFFFF").toLowerCase()}`;
  form.weight_g.value = spool?.weight_g ?? 1000;
  form.remaining_g.value = spool?.remaining_g ?? 1000;
  form.price_kg.value = spool?.price_kg ?? "";
  form.alert_g.value = spool?.alert_g ?? 100;
  $("sp-printer").innerHTML = `<option value="">En stock (rangée)</option>` + stock.printers.map(p =>
    `<option value="${p.id}" ${p.id === spool?.printer_id ? "selected" : ""}>${BC.escapeHtml(p.name)}</option>`).join("");
  fillSlots(spool?.printer_id || "", spool?.slot);
  $("spool-archive").hidden = !spool;
  $("spool-editor").hidden = false;
  form.name.focus();
}

async function saveSpool(e) {
  e.preventDefault();
  const form = $("spool-form");
  const body = Object.fromEntries(new FormData(form).entries());
  body.color = (body.color || "").replace("#", "");
  if (!body.printer_id) body.slot = "";
  const url = editing ? `/api/spools/${editing.id}` : "/api/spools";
  const { ok, json } = await BC.api(url, { method: "POST", body });
  BC.toast(ok ? "ok" : "err", json.message || json.error || "Erreur");
  if (ok) {
    $("spool-editor").hidden = true;
    loadSpools();
  }
}

async function archiveSpool() {
  if (!editing) return;
  const ok = await BC.confirmDialog({
    title: `Retirer « ${editing.name} » du stock ?`,
    message: "À faire quand la bobine est terminée ou jetée. Elle n'apparaîtra plus dans la liste.",
    confirmLabel: "Retirer",
  });
  if (!ok) return;
  const { ok: done, json } = await BC.api(`/api/spools/${editing.id}/archive`, { method: "POST", body: {} });
  BC.toast(done ? "ok" : "err", json.message || json.error);
  $("spool-editor").hidden = true;
  loadSpools();
}

document.addEventListener("DOMContentLoaded", () => {
  loadSpools();
  $("spool-add").addEventListener("click", () => openEditor());
  $("spool-cancel").addEventListener("click", () => { $("spool-editor").hidden = true; });
  $("spool-form").addEventListener("submit", saveSpool);
  $("spool-archive").addEventListener("click", archiveSpool);
  $("sp-printer").addEventListener("change", e => fillSlots(e.target.value, null));
  $("spools").addEventListener("click", e => {
    const card = e.target.closest("[data-spool]");
    if (card) openEditor(stock.spools.find(s => s.id === Number(card.dataset.spool)));
  });
  setInterval(() => { if (!document.hidden && $("spool-editor").hidden) loadSpools(); }, 30000);
});
