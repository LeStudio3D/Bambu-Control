// ═══════════════════════════════════════════════════════════
// Bambu Control — settings.js (page Paramètres)
// État de connexion de chaque imprimante, modification, test, suppression.
// ═══════════════════════════════════════════════════════════

const CONN_LABELS = { online: "Connectée", connecting: "Connexion", offline: "Hors ligne" };

// ── État de connexion de chaque imprimante ──
async function refreshPrinters() {
  try {
    const { json } = await BC.api("/api/printers");
    json.forEach(p => {
      const row = document.querySelector(`[data-printer="${p.id}"]`);
      if (!row) return;
      const el = row.querySelector("[data-conn]");
      el.className = `conn ${p.connection}`;
      el.title = p.last_error || "";
      row.querySelector("[data-conn-label]").textContent = CONN_LABELS[p.connection] || "Hors ligne";
    });
  } catch (e) { /* on réessaie au prochain tour */ }
}

// ── Résultat du test de connexion ──
function renderChecks(box, result) {
  box.hidden = false;
  if (result.error) {
    box.innerHTML = `<div class="check-item fail"><span class="mark">✕</span><div><strong>Test impossible</strong>
      <span>${BC.escapeHtml(result.error)}</span></div></div>`;
    return;
  }
  const items = result.checks.map(c => {
    const cls = c.ok ? "ok" : c.critical ? "fail" : "warn";
    const mark = c.ok ? "✓" : c.critical ? "✕" : "!";
    return `<div class="check-item ${cls}"><span class="mark">${mark}</span>
      <div><strong>${BC.escapeHtml(c.name)}</strong><span>${BC.escapeHtml(c.detail)}</span></div></div>`;
  }).join("");
  const summary = result.ok
    ? `<div class="check-item ok"><span class="mark">✓</span><div><strong>Connexion réussie</strong><span>Les réglages sont bons.</span></div></div>`
    : "";
  box.innerHTML = items + summary;
}

async function runTest(box, button, body) {
  box.hidden = false;
  box.innerHTML = `<div class="muted">Test en cours (jusqu'à 15 secondes)…</div>`;
  button.disabled = true;
  try {
    const { json } = await BC.api("/api/printers/test", { method: "POST", body });
    renderChecks(box, json);
  } catch (e) {
    renderChecks(box, { error: "Bambu Control ne répond pas (le serveur est-il lancé ?)." });
  } finally {
    button.disabled = false;
  }
}

function formValues(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  return { ip: data.ip || "", access_code: data.access_code || "", serial: data.serial || "", model: data.model };
}

function setupTests() {
  // Imprimante enregistrée : le serveur utilise les réglages enregistrés
  document.querySelectorAll("[data-test-saved]").forEach(btn => btn.addEventListener("click", () => {
    const id = btn.dataset.testSaved;
    runTest(document.querySelector(`[data-results="${id}"]`), btn, { printer_id: id });
  }));
  // Formulaire (ajout ou modification) : on teste ce qui est saisi
  document.querySelectorAll("[data-test-form]").forEach(btn => btn.addEventListener("click", () => {
    const form = btn.closest("form");
    const id = form.dataset.edit;
    const box = id ? document.querySelector(`[data-results="${id}"]`) : document.getElementById("add-results");
    runTest(box, btn, { ...formValues(form), printer_id: id || undefined });
  }));
}

function setupEditToggles() {
  document.querySelectorAll("[data-edit-toggle]").forEach(btn => btn.addEventListener("click", () => {
    const form = document.querySelector(`[data-edit="${btn.dataset.editToggle}"]`);
    form.hidden = !form.hidden;
    btn.textContent = form.hidden ? "Modifier" : "Fermer";
  }));
}

function setupDeleteConfirmations() {
  document.querySelectorAll("form[data-delete]").forEach(form => {
    form.addEventListener("submit", async e => {
      if (form.dataset.confirmed) return;
      e.preventDefault();
      const ok = await BC.confirmDialog({
        title: `Supprimer « ${form.dataset.delete} » ?`,
        message: "La connexion est coupée. Son historique reste en base mais ne sera plus affiché.",
        confirmLabel: "Supprimer",
      });
      if (ok) { form.dataset.confirmed = "1"; form.submit(); }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupDeleteConfirmations();
  setupEditToggles();
  setupTests();
  refreshPrinters();
  setInterval(() => { if (!document.hidden) refreshPrinters(); }, 3000);
});

// ── Intelligence artificielle ──
// Deux blocs « service » (texte et images), chacun avec son fournisseur, son
// adresse, sa clé et son modèle. Case « même service » : le bloc images ne
// garde que le choix du modèle et utilise la connexion du bloc texte.
function setupAiService(root, textRoot, sameBox) {
  const $ = sel => root.querySelector(sel);
  const provider = $("[data-ai-provider]");
  const url = $("[data-ai-url]");
  const out = $("[data-ai-result]");
  const button = $("[data-ai-test]");
  const isVision = root.dataset.aiService === "vision";
  const usesText = () => isVision && sameBox.checked;

  const updatePlaceholder = () => { url.placeholder = provider.selectedOptions[0].dataset.url; };
  provider.addEventListener("change", updatePlaceholder);
  updatePlaceholder();

  if (isVision) {
    // Champs de connexion masqués quand on réutilise le service texte
    const connFields = [provider, url, $("[data-ai-key]")].map(el => el.closest(".field"));
    const sync = () => connFields.forEach(f => { f.hidden = sameBox.checked; });
    sameBox.addEventListener("change", sync);
    sync();
  }

  button.addEventListener("click", async () => {
    const source = usesText() ? textRoot : root;
    out.textContent = "Test en cours…";
    out.style.color = "";
    button.disabled = true;
    try {
      const { ok, json } = await BC.api("/api/ai/test", { method: "POST", body: {
        target: source.dataset.aiService,
        provider: source.querySelector("[data-ai-provider]").value,
        base_url: source.querySelector("[data-ai-url]").value,
        api_key: source.querySelector("[data-ai-key]").value,
      } });
      if (ok) {
        root.querySelector("datalist").innerHTML =
          json.models.map(m => `<option value="${BC.escapeHtml(m)}"></option>`).join("");
        out.textContent = `${json.message} Choisis-en un dans le champ « Modèle ».`;
        out.style.color = "var(--ok)";
      } else {
        out.textContent = json.error || "Échec du test.";
        out.style.color = "var(--danger)";
      }
    } catch (e) {
      out.textContent = "Bambu Control ne répond pas (le serveur est-il lancé ?).";
      out.style.color = "var(--danger)";
    } finally {
      button.disabled = false;
    }
  });
}

function setupAi() {
  const textRoot = document.querySelector('[data-ai-service="text"]');
  if (!textRoot) return;
  const sameBox = document.getElementById("ai-vision-same");
  document.querySelectorAll("[data-ai-service]").forEach(root => setupAiService(root, textRoot, sameBox));
}
document.addEventListener("DOMContentLoaded", setupAi);
