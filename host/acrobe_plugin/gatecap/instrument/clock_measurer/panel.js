// Clock-measurer panel: the current rate of every observed clock, plus a
// rolling backlog of those rates drawn as curves.
//
// The rates are the whole state of the instrument, so they ride the status
// poll: the driver's poll() carries them, the shell paints the pill from the
// same answer, and the pane draws what it is handed. Nothing here polls on its
// own, and a hidden pane costs no traffic beyond the pill's.
//
// Curves of clocks orders of magnitude apart cannot share one y-axis
// usefully, so the axis is scaled to the *selected* clocks alone and the
// selection is per-instrument persistent state.
(function () {
  const CLOCK_MEASURER_UUID = "ba9af9d4-8767-4567-8e56-01bb12307fb7";

  // Backlog depth, in samples. At the shell's poll period this is a few
  // minutes of history; the graph is a trend, not a record.
  const BACKLOG = 240;

  const COLORS = ["#4ea1ff", "#ff9f43", "#5ad469", "#e15c7a",
                  "#b98cff", "#3fd0c9", "#d8c545", "#8d99ae"];

  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // Rates span kHz to GHz within one pane; a fixed unit would make one of them
  // unreadable, so each value picks its own.
  function rate(hz) {
    if (hz >= 1e9) return (hz / 1e9).toFixed(6) + " GHz";
    if (hz >= 1e6) return (hz / 1e6).toFixed(3) + " MHz";
    if (hz >= 1e3) return (hz / 1e3).toFixed(3) + " kHz";
    return hz + " Hz";
  }

  const impl = {
    async render(ctx, el, instrument) {
      const saved = await ctx.settingsGet();
      // First run selects everything: a pane that opens empty looks broken.
      const selected = Array.isArray(saved.selected)
        ? instrument.clock_names.filter((n) => saved.selected.includes(n))
        : instrument.clock_names.slice();

      ctx.state.history = {};              // clock name -> [rate, ...]
      instrument.clock_names.forEach((n) => { ctx.state.history[n] = []; });
      ctx.state.selected = new Set(selected);
      ctx.state.el = el;

      el.innerHTML =
        `<div class="pane-controls">`
        + `<span><span class="name">${esc(instrument.name)}</span>`
        + `<span class="kind">clock-measurer</span></span>`
        + `<span class="fld"><label>reference</label>`
        + `<span>${esc(instrument.reference_name)} @ `
        + `${rate(instrument.reference_hz)}</span></span>`
        + `<span class="fld"><label>refresh</label>`
        + `<span>${instrument.update_hz}/s, to `
        + `${rate(instrument.quantum_hz)}</span></span></div>`
        + `<table data-rates><tbody>`
        + instrument.clock_names.map((name, i) =>
            `<tr><td><label><input type="checkbox" data-clock="${esc(name)}"`
            + `${ctx.state.selected.has(name) ? " checked" : ""}>`
            + `<span style="color:${COLORS[i % COLORS.length]}">&#9632;</span> `
            + `${esc(name)}</label></td>`
            + `<td data-value="${esc(name)}">-</td></tr>`).join("")
        + `</tbody></table>`
        + `<canvas data-graph height="160"></canvas>`;

      el.querySelectorAll("[data-clock]").forEach((box) => {
        box.onchange = async () => {
          if (box.checked) ctx.state.selected.add(box.dataset.clock);
          else ctx.state.selected.delete(box.dataset.clock);
          const saved = await ctx.settingsGet();
          await ctx.settingsSet(
            Object.assign({}, saved, { selected: [...ctx.state.selected] }));
          this.draw(ctx, instrument);
        };
      });
    },

    // Every poll carries the whole rate set, which is everything this
    // instrument holds: append it to the backlog and redraw.
    onStatus(ctx, instrument, status) {
      if (!status.rates || !ctx.state.el) return;
      for (const name of instrument.clock_names) {
        const series = ctx.state.history[name];
        series.push(status.rates[name] || 0);
        if (series.length > BACKLOG) series.shift();
      }
      this.draw(ctx, instrument);
    },

    draw(ctx, instrument) {
      // Scoped to this pane's own element: a rack may hold several measurers,
      // and their tables carry the same clock names.
      const el = ctx.state.el;
      if (!el) return;
      for (const name of instrument.clock_names) {
        const series = ctx.state.history[name];
        const cell = el.querySelector(`[data-value="${CSS.escape(name)}"]`);
        if (cell) cell.textContent = series.length
          ? rate(series[series.length - 1]) : "-";
      }

      const canvas = el.querySelector("[data-graph]");
      const width = canvas.clientWidth || canvas.width;
      canvas.width = width;                      // resets the drawing buffer
      const height = canvas.height;
      const g = canvas.getContext("2d");
      g.clearRect(0, 0, width, height);

      const shown = instrument.clock_names
        .map((name, i) => ({ name, color: COLORS[i % COLORS.length],
                             series: ctx.state.history[name] }))
        .filter((s) => ctx.state.selected.has(s.name) && s.series.length);
      if (!shown.length) return;

      // The axis covers the selected clocks only: that is the whole point of
      // the selection, since unrelated magnitudes flatten each other out.
      let lo = Infinity, hi = -Infinity;
      for (const s of shown) for (const v of s.series) {
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      // A dead-flat trace would divide by zero; give it a band to sit in.
      if (hi === lo) { hi = lo + Math.max(1, Math.abs(lo) * 1e-6); }
      const pad = (hi - lo) * 0.1;
      lo -= pad; hi += pad;

      const left = 74, top = 6, bottom = height - 16;
      const x = (i) => left + (width - left - 6)
        * (BACKLOG <= 1 ? 0 : i / (BACKLOG - 1));
      const y = (v) => bottom - (bottom - top) * (v - lo) / (hi - lo);

      g.strokeStyle = "#8888";
      g.fillStyle = "#888";
      g.font = "10px monospace";
      g.textAlign = "right";
      for (const frac of [0, 0.5, 1]) {
        const value = lo + (hi - lo) * frac;
        const py = Math.round(y(value)) + 0.5;
        g.beginPath(); g.moveTo(left, py); g.lineTo(width - 6, py); g.stroke();
        g.fillText(rate(Math.max(0, Math.round(value))), left - 4, py + 3);
      }

      g.lineWidth = 1.5;
      for (const s of shown) {
        // The backlog fills from the right, so the newest sample is always at
        // the same place while history builds up.
        const offset = BACKLOG - s.series.length;
        g.strokeStyle = s.color;
        g.beginPath();
        s.series.forEach((v, i) => {
          const px = x(offset + i), py = y(v);
          if (i === 0) g.moveTo(px, py); else g.lineTo(px, py);
        });
        g.stroke();
      }
    },
  };

  window.gatecap.registerPanel(CLOCK_MEASURER_UUID, impl);
})();
