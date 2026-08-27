// Control/status panel: one pane for the whole instrument, built from the
// inventory the descriptor carries. Every widget is a pure function of kind,
// width and enum binding -- a control of one bit is a checkbox, a wider one a
// hex/binary entry (double-click its name to switch base), an enum-bound one a
// drop-down; a status is an LED, a value label or a decoded label the same
// way; a tick output is a push button, and a word packing several of them also
// offers check-then-confirm so they strobe in one write, which is what makes
// them simultaneous; a tick input is a counter readout that flashes while its
// sticky bit says something happened, with a reset that clears both.
//
// Controls are written on user action and read back once, when the pane is
// built: they are the only state the hardware holds that the host cannot
// recompute. Everything else on screen comes from the status poll, which reads
// the panel's whole live state in one burst -- so the pane never polls
// anything itself.
(function () {
  const CONTROL_STATUS_UUID = "dd241b36-f1b0-4418-b6b8-23223e5a93ff";

  // The pane styles itself: an instrument ships its own presentation, and the
  // shell knows nothing of panels. Injected once, whatever the pane count.
  const STYLE = `
  .cs-group { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap;
              padding: .3rem .7rem; border-bottom: 1px solid #2c2c2c; }
  .cs-group > .cs-title { color: #7fd1ff; font-size: .78rem; min-width: 5.5rem; }
  .cs-sig { display: flex; align-items: center; gap: .3rem;
            border: 1px solid #3a3a3a; border-radius: 4px; padding: .1rem .35rem; }
  .cs-sig > label { font: 11px/1.4 monospace; color: #cdd; white-space: nowrap; }
  .cs-sig.based > label { cursor: pointer; text-decoration: underline dotted #789; }
  .cs-sig input.val { width: 5.5rem; font-family: monospace; }
  .cs-sig input.val.bad { color: #ff7a7a; }
  .cs-sig .ro { font: 11px/1.4 monospace; color: #9fe; min-width: 3rem; }
  .cs-sig .count { font: 11px/1.4 monospace; color: #9fe; min-width: 3.5rem; }
  .cs-sig.hit { background: #4a3d00; }
  .cs-sig button { padding: .1rem .45rem; font-size: .78rem; }
  .cs-word { display: flex; align-items: center; gap: .3rem;
             border: 1px solid #3a3a3a; border-radius: 4px; padding: .1rem .35rem; }
  .cs-word .sep { color: #666; }
  `;

  const ensureCss = () => {
    if (document.getElementById("cs-style")) return;
    const s = document.createElement("style");
    s.id = "cs-style";
    s.textContent = STYLE;
    document.head.appendChild(s);
  };

  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const hasEnum = (f) => f.enum && Object.keys(f.enum).length > 0;
  const fmt = (value, base, width) => base === "bin"
    ? value.toString(2).padStart(width, "0")
    : "0x" + value.toString(16);

  const impl = {
    async render(ctx, el, block) {
      ensureCss();
      const saved = await ctx.settingsGet();
      ctx.state.bases = saved.bases || {};      // per-signal display base
      let html = "";
      if (block.controls.length)
        html += this.group("controls", block.controls.map(
          (f, i) => this.controlWidget(ctx, f, i)).join(""));
      if (block.statuses.length)
        html += this.group("statuses", block.statuses.map(
          (f, i) => this.statusWidget(ctx, f, i)).join(""));
      if (block.tick_out.length)
        html += this.group("tick out", block.tick_out.map(
          (w, i) => this.tickOutWord(w, i)).join(""));
      if (block.tick_in.length)
        html += this.group("tick in", block.tick_in.map(
          (w) => w.map(n => this.tickInWidget(n)).join("")).join(""));
      el.innerHTML =
        `<div class="pane-controls">`
        + `<span><span class="name">${esc(block.name)}</span>`
        + `<span class="kind">control/status</span></span></div>`
        + html;
      ctx.state.el = el;
      this.wire(ctx, el, block);
      await this.sync(ctx, el, block);
    },

    group(title, body) {
      return `<div class="cs-group"><span class="cs-title">${title}</span>`
        + body + `</div>`;
    },

    // -- controls ---------------------------------------------------------

    controlWidget(ctx, f, i) {
      const label = `<label data-cl="${i}" title="${f.width} bit(s)">`
        + `${esc(f.name)}</label>`;
      if (hasEnum(f)) {
        const opts = Object.keys(f.enum).sort((a, b) => a - b).map(
          v => `<option value="${v}">${esc(f.enum[v])}</option>`).join("");
        return `<span class="cs-sig">${label}`
          + `<select data-ce="${i}">${opts}</select></span>`;
      }
      if (f.width === 1)
        return `<span class="cs-sig">${label}`
          + `<input type="checkbox" data-cb="${i}"></span>`;
      const base = ctx.state.bases[f.name] || "hex";
      return `<span class="cs-sig based" data-base="${base}">${label}`
        + `<input class="val" data-cv="${i}" value="${base === "bin" ? "0" : "0x0"}"`
        + ` title="double-click the name to switch hex/binary"></span>`;
    },

    // -- statuses ---------------------------------------------------------

    statusWidget(ctx, f, i) {
      const label = `<label data-sl="${i}" title="${f.width} bit(s)">`
        + `${esc(f.name)}</label>`;
      if (hasEnum(f))
        return `<span class="cs-sig">${label}`
          + `<span class="ro" data-sv="${i}">-</span></span>`;
      if (f.width === 1)
        return `<span class="cs-sig">${label}`
          + `<span class="led gray" data-sd="${i}"></span></span>`;
      const base = ctx.state.bases[f.name] || "hex";
      return `<span class="cs-sig based" data-base="${base}">${label}`
        + `<span class="ro" data-sv="${i}">-</span></span>`;
    },

    // -- ticks ------------------------------------------------------------

    // One push button per tick. A word packing several of them also offers a
    // checkbox each and a confirm button: that write is the simultaneity
    // guarantee, and it only exists within one word.
    tickOutWord(names, word) {
      const buttons = names.map(
        (n, b) => `<button data-tk="${word}:${b}">${esc(n)}</button>`).join("");
      if (names.length < 2)
        return `<span class="cs-word">${buttons}</span>`;
      const boxes = names.map(
        (n, b) => `<label title="strobe ${esc(n)} with the checked set">`
          + `<input type="checkbox" data-tg="${word}:${b}">${esc(n)}</label>`)
        .join("");
      return `<span class="cs-word">${buttons}<span class="sep">|</span>`
        + boxes
        + `<button data-tc="${word}" title="strobe every checked tick of this `
        + `word in one write, so they assert in the same cycle">together`
        + `</button></span>`;
    },

    tickInWidget(name) {
      return `<span class="cs-sig" data-ti="${esc(name)}">`
        + `<label>${esc(name)}</label>`
        + `<span class="count" data-tv="${esc(name)}">0</span>`
        + `<button data-tr="${esc(name)}" title="clear the sticky bit and `
        + `rebase the counter">reset</button></span>`;
    },

    // -- wiring -----------------------------------------------------------

    wire(ctx, el, block) {
      el.querySelectorAll("[data-cb]").forEach(box => {
        const f = block.controls[+box.dataset.cb];
        box.onchange = () => this.write(ctx, f, box.checked ? 1 : 0);
      });
      el.querySelectorAll("[data-ce]").forEach(sel => {
        const f = block.controls[+sel.dataset.ce];
        sel.onchange = () => this.write(ctx, f, parseInt(sel.value, 10));
      });
      el.querySelectorAll("[data-cv]").forEach(inp => {
        const f = block.controls[+inp.dataset.cv];
        inp.oninput = () => {
          const value = this.parse(inp, f, this.baseOf(inp));
          if (value !== null) this.write(ctx, f, value);
        };
      });
      // Double-click a name to switch that signal between hex and binary.
      el.querySelectorAll("[data-cl],[data-sl]").forEach(lbl => {
        const list = lbl.dataset.cl != null ? block.controls : block.statuses;
        const f = list[+(lbl.dataset.cl != null ? lbl.dataset.cl : lbl.dataset.sl)];
        if (f.width === 1 || hasEnum(f)) return;
        lbl.ondblclick = () => this.toggleBase(ctx, lbl, f);
      });
      el.querySelectorAll("[data-tk]").forEach(btn => {
        const [word, bit] = btn.dataset.tk.split(":").map(Number);
        btn.onclick = () => this.strobe(ctx, block, [block.tick_out[word][bit]]);
      });
      el.querySelectorAll("[data-tc]").forEach(btn => {
        const word = +btn.dataset.tc;
        btn.onclick = () => {
          const names = block.tick_out[word].filter((_, b) =>
            el.querySelector(`[data-tg="${word}:${b}"]`).checked);
          if (names.length) this.strobe(ctx, block, names);
        };
      });
      el.querySelectorAll("[data-tr]").forEach(btn => {
        const name = btn.dataset.tr;
        btn.onclick = async () => {
          const r = await ctx.send({ op: "reset", names: [name] });
          if (r.error) ctx.log("reset failed: " + r.error);
        };
      });
    },

    baseOf(node) {
      const box = node.closest(".cs-sig");
      return (box && box.dataset.base) || "hex";
    },

    async toggleBase(ctx, label, f) {
      const box = label.closest(".cs-sig");
      const base = box.dataset.base === "bin" ? "hex" : "bin";
      box.dataset.base = base;
      ctx.state.bases[f.name] = base;
      const input = box.querySelector("input.val");
      if (input) {
        const value = this.parse(input, f, base === "bin" ? "hex" : "bin");
        input.value = fmt(value === null ? 0 : value, base, f.width);
        input.classList.remove("bad");
      }
      await ctx.settingsSet({ bases: ctx.state.bases });
    },

    // The entry's value, or null when it is not a number of its base or does
    // not fit the field (the widget says so and nothing is written).
    parse(input, f, base) {
      const text = input.value.trim();
      const ok = base === "bin" ? /^[01]+$/.test(text)
                                : /^(0x)?[0-9a-f]+$/i.test(text);
      const value = ok ? parseInt(text.replace(/^0x/i, ""),
                                  base === "bin" ? 2 : 16) : NaN;
      const bad = !ok || !(value >= 0) || value > 2 ** f.width - 1;
      input.classList.toggle("bad", bad);
      return bad ? null : value;
    },

    async write(ctx, f, value) {
      const r = await ctx.send({ op: "control", name: f.name, value });
      if (r.error) ctx.log("control write failed: " + r.error);
      else logConfig(ctx, { name: f.name }, r.summary);
    },

    async strobe(ctx, block, names) {
      const r = await ctx.send({ op: "tick", names });
      if (r.error) ctx.log("strobe failed: " + r.error);
      else ctx.log(block.name + ": " + r.summary);
    },

    // Initial widget state: the controls the hardware still holds. This is the
    // only read the pane makes on its own -- everything else rides the poll.
    async sync(ctx, el, block) {
      const r = await ctx.send({ op: "controls" });
      if (r.error) { ctx.log("control readback failed: " + r.error); return; }
      block.controls.forEach((f, i) => {
        const value = r.values[f.name];
        if (value == null) return;
        const box = el.querySelector(`[data-cb="${i}"]`);
        if (box) { box.checked = !!value; return; }
        const sel = el.querySelector(`[data-ce="${i}"]`);
        if (sel) {
          // A value no label binds is still what the hardware holds: show it
          // as a number rather than the first label of the table.
          if (!sel.querySelector(`option[value="${value}"]`)) {
            const opt = document.createElement("option");
            opt.value = String(value);
            opt.textContent = fmt(value, "hex", f.width);
            sel.appendChild(opt);
          }
          sel.value = String(value);
          return;
        }
        const inp = el.querySelector(`[data-cv="${i}"]`);
        if (inp) inp.value = fmt(value, this.baseOf(inp), f.width);
      });
    },

    // Every poll carries the whole live panel: the status levels, the sticky
    // bits and the counters. A sticky bit is the change indicator -- flash the
    // counter, then acknowledge exactly the bits just seen, so an event that
    // lands in between is not cleared unread.
    onStatus(ctx, block, status) {
      // Scoped to this pane's own element: a rack may hold several panels, and
      // their widgets carry the same signal names.
      const root = ctx.state.el;
      if (!root) return;
      block.statuses.forEach((f, i) => {
        const value = status.status ? status.status[f.name] : null;
        if (value == null) return;
        const led = root.querySelector(`[data-sd="${i}"]`);
        if (led) { led.className = "led " + (value ? "green" : "gray"); return; }
        const out = root.querySelector(`[data-sv="${i}"]`);
        if (!out) return;
        out.textContent = hasEnum(f)
          ? (f.enum[String(value)] || fmt(value, "hex", f.width))
          : fmt(value, this.baseOf(out), f.width);
      });
      const seen = [];
      Object.entries(status.counters || {}).forEach(([name, count]) => {
        const out = root.querySelector(`[data-tv="${CSS.escape(name)}"]`);
        if (out) out.textContent = String(count);
      });
      Object.entries(status.sticky || {}).forEach(([name, pending]) => {
        const box = root.querySelector(`[data-ti="${CSS.escape(name)}"]`);
        if (box) box.classList.toggle("hit", !!pending);
        if (pending) seen.push(name);
      });
      if (seen.length) ctx.send({ op: "ack", names: seen });
    },
  };

  window.gatecap.registerPanel(CONTROL_STATUS_UUID, impl);
})();
