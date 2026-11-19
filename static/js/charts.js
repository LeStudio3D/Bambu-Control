// ═══════════════════════════════════════════════════════════
// Bambu Control — charts.js
// Petit graphique en courbes (SVG), sans librairie externe :
// grille discrète, légende, curseur vertical + infobulle au survol.
// Une seule échelle verticale par graphique (jamais deux axes).
// ═══════════════════════════════════════════════════════════

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
  return el;
}

function formatTime(iso, withDay) {
  const d = new Date(iso);
  const time = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  return withDay ? `${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })} ${time}` : time;
}

function niceMax(value) {
  if (value <= 0) return 10;
  const step = Math.pow(10, Math.floor(Math.log10(value)));
  return Math.ceil(value / step) * step;
}

/**
 * Dessine un graphique en courbes dans `container`.
 * options = { rows, series: [{key, label, color}], unit, yMax }
 */
function drawLineChart(container, options) {
  const { rows, series, unit = "", yMax } = options;
  container.innerHTML = "";
  if (!rows || rows.length < 2) {
    container.innerHTML = `<div class="chart-empty">Pas encore assez de relevés pour tracer une courbe.</div>`;
    return;
  }

  const width = container.clientWidth || 600;
  const height = 220;
  const pad = { top: 12, right: 16, bottom: 26, left: 44 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const times = rows.map(r => new Date(r.timestamp).getTime());
  const t0 = times[0], t1 = times[times.length - 1] || t0 + 1;
  const values = rows.flatMap(r => series.map(s => r[s.key])).filter(v => v != null);
  const top = yMax ?? niceMax(Math.max(...values, 1));
  const x = t => pad.left + ((t - t0) / Math.max(t1 - t0, 1)) * plotW;
  const y = v => pad.top + plotH - (Math.min(v, top) / top) * plotH;

  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "chart-svg",
                             role: "img", "aria-label": series.map(s => s.label).join(", ") });

  // Grille et graduations (discrètes)
  for (let i = 0; i <= 4; i++) {
    const value = (top / 4) * i;
    const gy = y(value);
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy, class: "chart-grid" }));
    const label = svgEl("text", { x: pad.left - 8, y: gy + 4, class: "chart-axis", "text-anchor": "end" });
    label.textContent = `${Math.round(value)}${unit}`;
    svg.appendChild(label);
  }
  const multiDay = t1 - t0 > 20 * 3600 * 1000;
  [0, 0.5, 1].forEach(f => {
    const t = t0 + f * (t1 - t0);
    const label = svgEl("text", { x: x(t), y: height - 6, class: "chart-axis",
                                  "text-anchor": f === 0 ? "start" : f === 1 ? "end" : "middle" });
    label.textContent = formatTime(new Date(t).toISOString(), multiDay);
    svg.appendChild(label);
  });

  // Courbes : un trou dans les données coupe la ligne au lieu de la relier
  series.forEach(s => {
    let d = "";
    let penDown = false;
    rows.forEach((r, i) => {
      const v = r[s.key];
      if (v == null) { penDown = false; return; }
      d += `${penDown ? "L" : "M"}${x(times[i]).toFixed(1)},${y(v).toFixed(1)} `;
      penDown = true;
    });
    svg.appendChild(svgEl("path", { d, class: "chart-line", stroke: s.color }));
  });

  // Curseur + infobulle
  const cursor = svgEl("line", { y1: pad.top, y2: pad.top + plotH, class: "chart-cursor", visibility: "hidden" });
  svg.appendChild(cursor);
  const dots = series.map(s => {
    const dot = svgEl("circle", { r: 4, fill: s.color, class: "chart-dot", visibility: "hidden" });
    svg.appendChild(dot);
    return dot;
  });
  const hit = svgEl("rect", { x: pad.left, y: pad.top, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(hit);

  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;

  function showAt(clientX) {
    const box = svg.getBoundingClientRect();
    const t = t0 + ((clientX - box.left - pad.left) / plotW) * (t1 - t0);
    let index = 0;
    times.forEach((tt, i) => { if (Math.abs(tt - t) < Math.abs(times[index] - t)) index = i; });
    const row = rows[index];
    const cx = x(times[index]);
    cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx);
    cursor.setAttribute("visibility", "visible");
    series.forEach((s, i) => {
      const v = row[s.key];
      dots[i].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { dots[i].setAttribute("cx", cx); dots[i].setAttribute("cy", y(v)); }
    });
    tooltip.innerHTML = `<div class="chart-tooltip-time">${formatTime(row.timestamp, true)}</div>` +
      series.map(s => `<div><span class="legend-swatch" style="background:${s.color}"></span>` +
        `${s.label} <strong>${row[s.key] == null ? "—" : Math.round(row[s.key]) + unit}</strong></div>`).join("");
    tooltip.hidden = false;
    const left = Math.min(cx + 12, width - tooltip.offsetWidth - 4);
    tooltip.style.left = `${Math.max(left, 0)}px`;
  }
  function hide() {
    tooltip.hidden = true;
    cursor.setAttribute("visibility", "hidden");
    dots.forEach(d => d.setAttribute("visibility", "hidden"));
  }
  hit.addEventListener("mousemove", e => showAt(e.clientX));
  hit.addEventListener("touchmove", e => { showAt(e.touches[0].clientX); e.preventDefault(); }, { passive: false });
  hit.addEventListener("mouseleave", hide);
  hit.addEventListener("touchend", hide);

  // Légende (toujours présente à partir de 2 courbes)
  if (series.length > 1) {
    const legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.innerHTML = series.map(s =>
      `<span><span class="legend-swatch" style="background:${s.color}"></span>${s.label}</span>`).join("");
    container.appendChild(legend);
  }
  const wrap = document.createElement("div");
  wrap.className = "chart-wrap";
  wrap.appendChild(svg);
  wrap.appendChild(tooltip);
  container.appendChild(wrap);
}
