// Logic-analyzer panel: the whole instrument on one pane, in the order its
// blocks are wired -- the trigger editors of the trigger blocks it holds, then
// the capture over its domains, then the waveform the read lands in. Editing a
// trigger writes the compare straight to the trigger hardware, so Arm only
// arms. All I/O goes through ctx (ctx.send routes to the analyzer's adaptor,
// which fans out to the blocks; ctx.Waveform is this panel's display surface).
//
// What the capture section offers follows the analyzer's shape. A correlated
// group is armed over a window in real time -- its domains sample at different
// rates, so a sample count would cover a different span on each of them --
// with a post-trigger cap and a pre-trigger ring in buffer lines for
// run-length-encoded members (the time a ring covers is what the captured data
// says, so it is not a duration). A single domain has nothing to be correlated
// with, so it is captured in its own samples, exactly as its control block
// counts them.
(function () {
  const LOGIC_ANALYZER_UUID = "ce4e395e-1439-4ab7-9cee-cfb4f3257f3d";

  const dec = (input) => parseInt(input.value, 10) || 0;
  const flt = (input) => parseFloat(input.value) || 0;
  const mhz = (rate) => rate ? (rate / 1e6) + " MHz" : "clock unknown";
  const hasEnum = (f) => f.enum && Object.keys(f.enum).length > 0;
  const fullMask = (f) => 2 ** f.width - 1;

  const fld = (key, label, val, title) => {
    const t = title ? ` title="${title}"` : "";
    return `<span class="fld"><label${t}>${label}</label>`
      + `<input data-${key} value="${val}"></span>`;
  };

  // ---- trigger editors ---------------------------------------------------
  // One per trigger flavour, keyed by the kind the driver reports. Each owns
  // its widgets, the per-field state it keeps across sessions, the summary
  // line, and the compare that state means; the panel only places them.

  // A value trigger: each scalar is don't-care/0/1, each bus a value/mask hex
  // pair or -- when it carries an enum -- a combo box of labels. Double-click
  // a bus label to switch that field between the combo and the raw entry.
  const ValueEditor = {
    rows(trigger, state) {
      return trigger.fields.map((f, fi) => {
        const st = state[fi];
        if (f.kind === "bit") {
          const sel = (typeof st === "string") ? st : "-";   // "-" = don't care
          return `<span class="sig"><label title="bit ${f.bit}">${f.name}</label>`
            + `<select data-fb="${fi}">`
            + ["-", "0", "1"].map(o =>
                `<option${o === sel ? " selected" : ""}>${o}</option>`).join("")
            + `</select></span>`;
        }
        return this.busRow(f, fi, st);
      }).join("");
    },

    busRow(f, fi, st) {
      const mode = hasEnum(f) ? ((st && st.mode) || "enum") : "raw";
      const hint = hasEnum(f) ? "double-click: enum / raw entry"
                              : `${f.width} bits, value/mask hex`;
      const label = `<label data-flabel="${fi}" title="${hint}">${f.name}</label>`;
      if (mode === "enum") {
        const sel = (st && st.sel != null) ? String(st.sel) : "-";
        const opts = [`<option value="-"${sel === "-" ? " selected" : ""}>-</option>`]
          .concat(Object.keys(f.enum).sort((a, b) => a - b).map(v =>
            `<option value="${v}"${sel === v ? " selected" : ""}>${f.enum[v]}</option>`));
        return `<span class="busfld enum">${label}`
          + `<select data-fe="${fi}">${opts.join("")}</select></span>`;
      }
      const v = (st && st.v) || "0", m = (st && st.m) || "0";
      return `<span class="busfld${hasEnum(f) ? " enum" : ""}">${label}`
        + `<input data-fv="${fi}" value="${v}">`
        + `<span class="sl">/</span>`
        + `<input data-fm="${fi}" value="${m}"></span>`;
    },

    wire(root, trigger, onEdit, onMode) {
      root.querySelectorAll("[data-fb],[data-fv],[data-fm],[data-fe]").forEach(inp =>
        inp.addEventListener(inp.tagName === "SELECT" ? "change" : "input", onEdit));
      root.querySelectorAll("[data-flabel]").forEach(lbl => {
        const fi = +lbl.dataset.flabel;
        if (hasEnum(trigger.fields[fi]))
          lbl.addEventListener("dblclick", () => onMode(fi));
      });
    },

    // Per-field UI state: "-"/"0"/"1" for a scalar; for a bus either
    // {mode:"enum", sel} (sel is "-" or a value string) or {mode:"raw", v, m}.
    states(root, trigger) {
      return trigger.fields.map((f, fi) => {
        if (f.kind === "bit") return root.querySelector(`[data-fb="${fi}"]`).value;
        const es = root.querySelector(`[data-fe="${fi}"]`);
        if (es) return { mode: "enum", sel: es.value };
        return { mode: "raw",
                 v: root.querySelector(`[data-fv="${fi}"]`).value,
                 m: root.querySelector(`[data-fm="${fi}"]`).value };
      });
    },

    // The state a mode switch produces for one field: enum -> raw seeds the
    // value/mask from the current selection; raw -> enum adopts the selection
    // when the value is a known label matched on the whole field.
    switched(f, s) {
      if (s.mode === "enum") {
        const ev = s.sel === "-" ? 0 : parseInt(s.sel, 10);
        return { mode: "raw", v: ev.toString(16),
                 m: (s.sel === "-" ? 0 : fullMask(f)).toString(16) };
      }
      const bv = parseInt(s.v, 16) || 0, bm = parseInt(s.m, 16) || 0;
      return { mode: "enum",
               sel: (bm === fullMask(f) && f.enum[bv] != null) ? String(bv) : "-" };
    },

    // Colour the raw bus inputs: red if not valid hex, amber if wider than the
    // field (still accepted -- params() only uses the field's own bits).
    validate(root, trigger) {
      trigger.fields.forEach((f, fi) => {
        if (f.kind !== "bus") return;
        const fv = root.querySelector(`[data-fv="${fi}"]`);
        if (!fv) return;   // enum combo mode: no raw inputs to check
        this.check(fv, f.width);
        this.check(root.querySelector(`[data-fm="${fi}"]`), f.width);
      });
    },

    check(input, width) {
      input.classList.remove("bad", "warn");
      const raw = input.value.trim();
      if (!/^(0x)?[0-9a-f]+$/i.test(raw)) { input.classList.add("bad"); return; }
      if (parseInt(raw, 16) > 2 ** width - 1) input.classList.add("warn");
    },

    summary(trigger, st) {
      const set = [];
      trigger.fields.forEach((f, fi) => {
        const s = st[fi];
        if (f.kind === "bit") { if (s !== "-") set.push(f.name + "=" + s); }
        else if (s.mode === "enum") {
          if (s.sel !== "-") set.push(f.name + "=" + f.enum[s.sel]);
        } else if (parseInt(s.m, 16)) set.push(f.name + "=" + s.v + "/" + s.m);
      });
      return set.length ? set.join(", ") : "any";
    },

    // value/mask from the fields. A scalar sets its bit; a bus distributes its
    // value/mask (or the selected enum value under a full mask) over its bits.
    // Trigger vectors are one APB word (<=32 bits), so 2**bit stays exact.
    params(trigger, st) {
      let value = 0, mask = 0;
      trigger.fields.forEach((f, fi) => {
        const s = st[fi];
        if (f.kind === "bit") {
          if (s === "1") { value += 2 ** f.bit; mask += 2 ** f.bit; }
          else if (s === "0") { mask += 2 ** f.bit; }
        } else if (s.mode === "enum") {
          if (s.sel === "-") return;
          const ev = parseInt(s.sel, 10);
          f.bits.forEach(([pos, bit]) => {
            if ((ev >> pos) & 1) value += 2 ** bit;
            mask += 2 ** bit;
          });
        } else {
          const bv = parseInt(s.v, 16) || 0, bm = parseInt(s.m, 16) || 0;
          f.bits.forEach(([pos, bit]) => {
            if ((bv >> pos) & 1) value += 2 ** bit;
            if ((bm >> pos) & 1) mask += 2 ** bit;
          });
        }
      });
      return { value, mask };
    },
  };

  // An edge trigger: per-signal don't-care / 0 / 1 / rising / falling, and per
  // bus a new value/mask plus an old value/mask -- an independent compare on
  // the current and the previous-cycle value.
  const EdgeEditor = {
    BITS: ["-", "0", "1", "↑", "↓"],

    rows(trigger, state) {
      return trigger.fields.map((f, fi) => {
        const s = state[fi];
        if (f.kind === "bit") {
          const sel = (typeof s === "string" && this.BITS.includes(s)) ? s : "-";
          return `<span class="sig"><label title="bit ${f.bit}">${f.name}</label>`
            + `<select data-fb="${fi}">`
            + this.BITS.map(o =>
                `<option${o === sel ? " selected" : ""}>${o}</option>`).join("")
            + `</select></span>`;
        }
        const nv = (s && s.nv) || "0", nm = (s && s.nm) || "0";
        const ov = (s && s.ov) || "0", om = (s && s.om) || "0";
        return `<span class="busfld" title="${f.width} bits: new value/mask, `
          + `old value/mask (hex)"><label>${f.name}</label>`
          + `<span class="sl">n</span><input data-nv="${fi}" value="${nv}">`
          + `<span class="sl">/</span><input data-nm="${fi}" value="${nm}">`
          + `<span class="sl">o</span><input data-ov="${fi}" value="${ov}">`
          + `<span class="sl">/</span><input data-om="${fi}" value="${om}"></span>`;
      }).join("");
    },

    wire(root, trigger, onEdit) {
      root.querySelectorAll("[data-fb],[data-nv],[data-nm],[data-ov],[data-om]")
        .forEach(inp => inp.addEventListener(
          inp.tagName === "SELECT" ? "change" : "input", onEdit));
    },

    states(root, trigger) {
      return trigger.fields.map((f, fi) => f.kind === "bit"
        ? root.querySelector(`[data-fb="${fi}"]`).value
        : { nv: root.querySelector(`[data-nv="${fi}"]`).value,
            nm: root.querySelector(`[data-nm="${fi}"]`).value,
            ov: root.querySelector(`[data-ov="${fi}"]`).value,
            om: root.querySelector(`[data-om="${fi}"]`).value });
    },

    validate() {},

    summary(trigger, st) {
      const lbl = { "0": "=0", "1": "=1", "↑": " ↑", "↓": " ↓" };
      const set = [];
      trigger.fields.forEach((f, fi) => {
        const s = st[fi];
        if (f.kind === "bit") { if (s !== "-") set.push(f.name + (lbl[s] || "")); }
        else if (parseInt(s.nm, 16) || parseInt(s.om, 16)) {
          let t = f.name + "=" + s.nv + "/" + s.nm;
          if (parseInt(s.om, 16)) t += " old " + s.ov + "/" + s.om;
          set.push(t);
        }
      });
      return set.length ? set.join(", ") : "any";
    },

    // The four compare vectors. A scalar sets its bit in the new (and, for an
    // edge, the old) compare; a bus distributes its new/old value/mask over
    // its bits.
    params(trigger, st) {
      let nv = 0, nm = 0, ov = 0, om = 0;
      trigger.fields.forEach((f, fi) => {
        if (f.kind === "bit") {
          const bit = 2 ** f.bit, s = st[fi];
          if (s === "1") { nv += bit; nm += bit; }
          else if (s === "0") { nm += bit; }
          else if (s === "↑") { nv += bit; nm += bit; om += bit; }   // old 0, new 1
          else if (s === "↓") { nm += bit; ov += bit; om += bit; }   // old 1, new 0
        } else {
          const bnv = parseInt(st[fi].nv, 16) || 0, bnm = parseInt(st[fi].nm, 16) || 0;
          const bov = parseInt(st[fi].ov, 16) || 0, bom = parseInt(st[fi].om, 16) || 0;
          f.bits.forEach(([pos, bit]) => {
            const b = 2 ** bit;
            if ((bnm >> pos) & 1) { nm += b; if ((bnv >> pos) & 1) nv += b; }
            if ((bom >> pos) & 1) { om += b; if ((bov >> pos) & 1) ov += b; }
          });
        }
      });
      return { new_value: nv, new_mask: nm, old_value: ov, old_mask: om };
    },
  };

  // A trigger flavour this panel has no editor for still gets the level one:
  // the fields are the same shape, and a value/mask compare is what every
  // trigger block accepts.
  const editorFor = (trigger) => trigger.kind === "edge" ? EdgeEditor : ValueEditor;

  const impl = {
    async render(ctx, el, instr) {
      ctx.state.saved = await ctx.settingsGet() || {};
      el.classList.add("grow");   // this panel owns a waveform and fills space
      el.innerHTML =
        this.headerRow(instr)
        + instr.triggers.map((t, ti) =>
            `<div class="pane-controls" data-trig="${ti}"></div>`).join("")
        + this.captureRow(instr, ctx.state.saved.capture || {})
        + `<div class="pane-wave"></div>`;
      instr.triggers.forEach((t, ti) => this.renderTrigger(ctx, el, instr, ti));
      this.wireCapture(ctx, el, instr);

      const wave = new ctx.Waveform(ctx.surferUrl);
      ctx.state.wave = wave;
      wave.mount(el.querySelector(".pane-wave"));
      this.hideSidePanel(ctx, wave);
      // Push the restored compares to the hardware: what the editors show is
      // what the next arm triggers on.
      await this.applyTriggers(ctx, el, instr);
    },

    // Every read adds all traces, so Surfer's hierarchy side panel is dead
    // space by default; the menu (Toggle side panel) brings it back.
    async hideSidePanel(ctx, wave) {
      for (let i = 0; i < 100 && !(await wave.ready()); i++) await ctx.sleep(100);
      if (await wave.ready()) wave.inject({ SetSidePanelVisible: false });
    },

    // -- the instrument and its domains -----------------------------------

    headerRow(instr) {
      const members = instr.members.map(m =>
        `${m.name} (${m.kind}, ${mhz(m.sample_rate)}, ${m.signal_count} probes)`)
        .join(" · ");
      const kind = instr.grouped
        ? `group of ${instr.members.length}, ${instr.composition}`
        : "logic analyzer";
      return `<div class="pane-controls">`
        + `<span><span class="name">${instr.name}</span>`
        + `<span class="kind">${kind}</span></span>`
        + `<span class="members">${members}</span></div>`;
    },

    // -- trigger sections --------------------------------------------------

    renderTrigger(ctx, el, instr, ti) {
      const trigger = instr.triggers[ti];
      const editor = editorFor(trigger);
      const root = this.triggerRoot(el, ti);
      const state = (ctx.state.saved.triggers || {})[trigger.name] || [];
      root.innerHTML =
        `<span><span class="name">${trigger.name}</span>`
        + `<span class="kind">${trigger.kind} trigger</span></span>`
        + `<details class="trig" open><summary>match: <span class="tsum"></span>`
        + `</summary><div class="trig-grid">${editor.rows(trigger, state)}</div>`
        + `</details>`;
      editor.wire(root, trigger,
                  () => this.onTriggerEdit(ctx, el, instr, ti),
                  (fi) => this.toggleMode(ctx, el, instr, ti, fi));
      editor.validate(root, trigger);
      this.showTriggerSummary(el, instr, ti);
    },

    triggerRoot(el, ti) {
      return el.querySelector(`[data-trig="${ti}"]`);
    },

    triggerStates(el, instr, ti) {
      const trigger = instr.triggers[ti];
      return editorFor(trigger).states(this.triggerRoot(el, ti), trigger);
    },

    showTriggerSummary(el, instr, ti) {
      const trigger = instr.triggers[ti];
      this.triggerRoot(el, ti).querySelector(".tsum").textContent =
        editorFor(trigger).summary(trigger, this.triggerStates(el, instr, ti));
    },

    async onTriggerEdit(ctx, el, instr, ti) {
      const trigger = instr.triggers[ti];
      this.showTriggerSummary(el, instr, ti);
      editorFor(trigger).validate(this.triggerRoot(el, ti), trigger);
      await this.applyTriggers(ctx, el, instr);
      await this.save(ctx, el, instr);
    },

    // A bus switching between its enum combo and raw entry: only that section
    // is rebuilt, so the waveform beside it survives the change.
    async toggleMode(ctx, el, instr, ti, fi) {
      const trigger = instr.triggers[ti];
      const editor = editorFor(trigger);
      const states = this.triggerStates(el, instr, ti);
      states[fi] = editor.switched(trigger.fields[fi], states[fi]);
      ctx.state.saved.triggers = Object.assign({}, ctx.state.saved.triggers,
                                               { [trigger.name]: states });
      await ctx.settingsSet(ctx.state.saved);
      this.renderTrigger(ctx, el, instr, ti);
      await this.applyTriggers(ctx, el, instr);
    },

    // Every trigger of the instrument in one message: the framework replays
    // the last "configure" after a reconnect, so a message that carries the
    // whole analyzer restores it whatever the user edited last.
    async applyTriggers(ctx, el, instr) {
      if (!instr.triggers.length) return;
      const triggers = {};
      instr.triggers.forEach((trigger, ti) => {
        triggers[trigger.name] = editorFor(trigger).params(
          trigger, this.triggerStates(el, instr, ti));
      });
      const r = await ctx.send({ op: "configure", triggers });
      if (r.error) { ctx.log("trigger configure failed: " + r.error); return; }
      logConfig(ctx, instr, r.summary);   // write is live; log the settled value
    },

    // -- capture section ---------------------------------------------------

    captureRow(instr, saved) {
      return `<div class="pane-controls" data-capture>`
        + `<span><span class="kind">capture</span></span>`
        + (instr.grouped ? this.groupFields(instr, saved)
                         : this.memberFields(instr, saved))
        // data-action lets the shell's C-l / C-S-l shortcuts click these too.
        + `<button data-action="arm">${instr.grouped ? "Arm group" : "Arm"}</button>`
        + `<button data-action="read">Read</button>`
        + `<button data-action="abort">Abort</button>`
        + `<span class="prog"></span>`   // live progress (updated on poll)
        // Readback progress: seconds of transfer on a slow link.
        + `<span class="fetch"><span class="bar"><i></i></span>`
        + `<span class="pct"></span></span>`
        + `<button data-action="reset-view" style="margin-left:auto" `
        + `title="discard the saved signal layout and rebuild the default `
        + `view: all signals, default order and radix, fresh markers">`
        + `Reset view</button>`
        // The download attribute forces a save dialog instead of navigation;
        // hidden until a capture has been read.
        + `<a data-action="vcd" download style="display:none" `
        + `title="download the displayed capture as a VCD file">VCD</a></div>`;
    },

    // A correlated group: the window in real time, and the ring depth of the
    // run-length-encoded members. A mixed group says which members each field
    // drives; a uniform one has a single vocabulary and needs no such label.
    groupFields(instr, saved) {
      const raw = instr.composition !== "rle";
      const rle = instr.composition !== "raw";
      const named = (kind) => instr.composition === "mixed"
        ? " (" + instr.members.filter(m => m.kind === kind)
                              .map(m => m.name).join(", ") + ")" : "";
      let params = "";
      if (raw) {
        params += fld("s", "span (µs)" + named("raw"), saved.span_us ?? "10",
                      "window length in real time; each member converts it "
                      + "with its own capture clock");
        params += fld("p", "pre (µs)" + named("raw"), saved.pre_us ?? "0",
                      "how much of the window precedes the trigger");
      } else {
        params += fld("s", "cap (µs)", saved.span_us ?? "10",
                      "post-trigger time cap (0 = until the buffer fills)");
      }
      if (rle) {
        params += fld("pl", "pre-lines" + named("rle"), saved.pre_lines ?? "16",
                      "pre-trigger ring of the run-length-encoded member(s), "
                      + "in buffer lines: the time it covers depends on the "
                      + "captured data");
      }
      if (instr.composition === "raw") {
        params += fld("w", "windows", saved.windows ?? "1");
      }
      return params;
    },

    // A single domain, in its control block's own parameters.
    memberFields(instr, saved) {
      if (instr.members[0].kind === "raw") {
        return fld("n", "count", saved.count ?? "64")
          + fld("p", "pre", saved.pre ?? "0")
          + fld("w", "windows", saved.windows ?? "1");
      }
      return fld("pl", "pre-lines", saved.pre_lines ?? "16")
        + fld("mc", "max-time (s)", saved.max_seconds ?? "1",
              "post-trigger time cap in seconds (0 = until the buffer fills)");
    },

    wireCapture(ctx, el, instr) {
      const box = el.querySelector("[data-capture]");
      box.querySelector('[data-action="arm"]').onclick =
        () => this.doArm(ctx, el, instr);
      box.querySelector('[data-action="read"]').onclick =
        () => this.doRead(ctx, instr, this.gather(el, instr));
      box.querySelector('[data-action="abort"]').onclick =
        () => ctx.send({ op: "abort" }).then(() => ctx.log("abort " + instr.name));
      box.querySelector('[data-action="reset-view"]').onclick =
        () => this.doResetView(ctx, instr);
      ctx.state.prog = box.querySelector(".prog");
      ctx.state.fetch = box.querySelector(".fetch");
      ctx.state.vcdLink = box.querySelector('[data-action="vcd"]');
    },

    // The capture parameters in the driver's vocabulary: a group window in
    // seconds (plus the RLE ring in buffer lines), or the one domain's own
    // parameters, handed to it as a per-member override.
    gather(el, instr) {
      const box = el.querySelector("[data-capture]");
      const q = (key) => box.querySelector(`[data-${key}]`);
      if (!instr.grouped) {
        const member = instr.members[0];
        const params = member.kind === "raw"
          ? { count: dec(q("n")), pretrigger: dec(q("p")),
              windows: dec(q("w")) || 1 }
          : { pre_lines: dec(q("pl")), max_seconds: flt(q("mc")) };
        return { overrides: { [member.name]: params } };
      }
      const params = {};
      // A span of 0 is "until the buffer fills", which only an RLE member can
      // honour; it is sent as no span at all, and a raw member refuses it.
      const span = flt(q("s")) * 1e-6;
      if (span > 0) params.seconds = span;
      if (q("p")) params.pre_seconds = flt(q("p")) * 1e-6;
      if (q("pl")) params.pre_lines = dec(q("pl"));
      if (q("w")) params.windows = dec(q("w")) || 1;
      return params;
    },

    // Restore-shaped settings (the field values as they stand).
    captureSettings(el, instr) {
      const box = el.querySelector("[data-capture]");
      const q = (key) => box.querySelector(`[data-${key}]`);
      if (!instr.grouped) {
        return instr.members[0].kind === "raw"
          ? { count: dec(q("n")), pre: dec(q("p")), windows: dec(q("w")) || 1 }
          : { pre_lines: dec(q("pl")), max_seconds: flt(q("mc")) };
      }
      const saved = { span_us: flt(q("s")) };
      if (q("p")) saved.pre_us = flt(q("p"));
      if (q("pl")) saved.pre_lines = dec(q("pl"));
      if (q("w")) saved.windows = dec(q("w")) || 1;
      return saved;
    },

    // One settings object for the whole panel, so a trigger edit and a capture
    // edit do not overwrite each other.
    async save(ctx, el, instr) {
      const triggers = {};
      instr.triggers.forEach((trigger, ti) => {
        triggers[trigger.name] = this.triggerStates(el, instr, ti);
      });
      ctx.state.saved.triggers = triggers;
      ctx.state.saved.capture = this.captureSettings(el, instr);
      await ctx.settingsSet(ctx.state.saved);
    },

    // Back to the default view: drop the persisted stackup and rebuild from
    // the last read's trace -- a Clear reload, every signal back in default
    // order and radix, fresh markers. Before any read there is nothing to
    // rebuild; the saved stackup is still dropped, so the next session
    // starts default too. The stackup snapshots then persist the rebuilt
    // view on their own.
    async doResetView(ctx, instr) {
      delete ctx.state.saved.surfer;
      delete ctx.state.saved.markerNames;
      ctx.state.stackupSnapshot = null;
      await ctx.settingsSet(ctx.state.saved);
      const wave = ctx.state.wave, last = ctx.state.lastLoad;
      if (!last || !(await wave.ready())) {
        ctx.log(`${instr.name}: saved view dropped; no trace to rebuild`);
        return;
      }
      wave.inject({ LoadWaveformFileFromUrl: [location.origin + last.url, "Clear"] });
      await ctx.sleep(150);
      ctx.state.markerNames = last.markers.map(m => m[0]);
      wave.runCommands(["scope_add_recursive capture", "zoom_fit"]
        .concat(last.markers.map(m => "marker_set " + m[0] + " " + m[1]))
        .join("\n") + "\n");
      ctx.log(`${instr.name}: view reset to default`);
    },

    // Arm returns as soon as every domain is armed; the trigger cannot fire
    // before that, so the DUT may be driven right after. The poll loop
    // auto-reads once the analyzer is back to idle.
    async doArm(ctx, el, instr) {
      const params = this.gather(el, instr);
      const r = await ctx.send({ op: "arm", params });
      if (r.error) { ctx.log("arm failed: " + r.error); return; }
      await this.save(ctx, el, instr);
      ctx.state.armed = params;
      ctx.log(`arm ${instr.name} <- ${r.summary || ""}; waiting for trigger`);
    },

    async doRead(ctx, instr, params) {
      // Show the bar as soon as the read is issued: the driver only reports
      // "reading" from the next poll on, up to a poll period later.
      ctx.state.fetching = true;
      this.showFetch(ctx, null);
      let res;
      try {
        res = await ctx.send({ op: "read", params });
      } finally {
        ctx.state.fetching = false;
        this.showFetch(ctx, null);
      }
      if (res.error) { ctx.log("read failed: " + res.error); return; }
      ctx.log(`read ${instr.name} (${res.scopes.join(", ")})`
              + (res.timescale ? ` on a ${res.timescale} timebase` : ""));
      // The trace exists server-side from here on, whatever Surfer does with
      // it below -- expose the download link now.
      const vcd = ctx.state.vcdLink;
      if (vcd) {
        vcd.href = res.trace_url;
        vcd.download = `${instr.name}-${res.serial}.vcd`;
        vcd.style.display = "";
      }
      // Load live via inject_message into this panel's own surface. KeepAll
      // reloads in place: the item tree (order, groups), colors, radix
      // choices, user markers and the viewport survive, and the displayed
      // variables re-bind by path into the new file (Surfer clips the kept
      // viewport into the new time range). The first load restores the
      // persisted stackup, or builds the default view (one recursive add of
      // the shared root brings in every domain's scope) when there is none
      // or the restore produced nothing -- Surfer's state format is not
      // stable across versions. Never both: a scope add fills its items in
      // asynchronously, so they land after the restore has replaced the
      // item list and everything shows twice. Our own markers are named;
      // marker_set resolves the name and moves the existing marker, and the
      // ones the previous read (or the restored stackup) placed that this
      // one does not are removed by name.
      const wave = ctx.state.wave;
      for (let i = 0; i < 100 && !(await wave.ready()); i++) await ctx.sleep(50);
      if (!(await wave.ready())) { ctx.log("Surfer not ready; try again"); return; }
      const first = !(await wave.wavesLoaded());
      wave.inject({ LoadWaveformFileFromUrl: [location.origin + res.trace_url, "KeepAll"] });
      if (first)
        for (let i = 0; i < 100 && !(await wave.wavesLoaded()); i++) await ctx.sleep(50);
      else
        await ctx.sleep(150);
      if (first) {
        let restored = false;
        if (ctx.state.saved.surfer) {
          wave.inject({ LoadStateFromData:
            Array.from(new TextEncoder().encode(ctx.state.saved.surfer)) });
          // LoadState is re-queued inside Surfer and a decode failure is
          // only logged there; probe the live state until a displayed item
          // shows up. A deliberately emptied stackup fails the probe and
          // gets the default view back -- acceptable for that corner.
          for (let i = 0; i < 20 && !restored; i++) {
            await ctx.sleep(100);
            restored = /Variable\(|Marker\(|Divider\(|Group\(|TimeLine\(|Stream\(|Placeholder\(/
              .test(await wave.state());
          }
        }
        if (!restored) wave.runCommands("scope_add_recursive capture\nzoom_fit\n");
      }
      const names = res.markers.map(m => m[0]);
      const prior = ctx.state.markerNames || ctx.state.saved.markerNames || [];
      const stale = prior.filter(n => !names.includes(n));
      ctx.state.markerNames = names;
      const cmds = stale.map(n => "marker_remove " + n)
        .concat(res.markers.map(m => "marker_set " + m[0] + " " + m[1]));
      wave.runCommands(cmds.join("\n") + "\n");
      ctx.state.lastLoad = { url: res.trace_url, markers: res.markers };
      ctx.state.stackupLive = true;
      ctx.log("loaded into Surfer");
    },

    // Live progress, refreshed each poll. The driver formats the string
    // (shared with the CLI); clear it when idle and never triggered.
    showProgress(ctx, status) {
      const out = ctx.state.prog;
      if (!out) return;
      out.textContent = (status.state === "idle" && !status.triggered)
        ? "" : (status.progress || "");
    },

    // The trace coming back over the transport: a determinate bar, driven by
    // the fetch fraction the driver counts (words moved / words planned).
    // `status` is null when the panel drives it itself (a read just issued).
    showFetch(ctx, status) {
      const box = ctx.state.fetch;
      if (!box) return;
      const reading = ctx.state.fetching || (status && status.state === "reading");
      box.classList.toggle("on", !!reading);
      const pct = Math.round(100 * ((status && status.fetch)
                                    ? status.fetch.fraction : 0));
      box.querySelector("i").style.width = pct + "%";
      box.querySelector(".pct").textContent = pct + "%";
    },

    // The shell forwards each poll here. Auto-read once the analyzer has
    // returned to idle on a trigger; aborted before the trigger just clears
    // the armed state. The stackup snapshot rides the poll too: the user
    // edits it inside Surfer, which emits no change events.
    onStatus(ctx, instr, status) {
      this.showProgress(ctx, status);
      this.showFetch(ctx, status);
      ctx.state.polls = (ctx.state.polls || 0) + 1;
      if (ctx.state.polls % 4 === 0) this.saveStackup(ctx);
      if (ctx.state.armed && status.state === "idle") {
        const params = ctx.state.armed; ctx.state.armed = null;
        if (status.triggered) this.doRead(ctx, instr, params);
        else ctx.log(`${instr.name}: returned to idle without a trigger`);
      }
    },

    // Persist Surfer's stackup next to the trigger and capture settings, so
    // the next session's first load restores it. Gated on stackupLive: a
    // snapshot taken before the first read has applied the saved stackup
    // would overwrite it with the default view. The marker names ride along
    // so the first read of the next session can retire the stale ones.
    async saveStackup(ctx) {
      const wave = ctx.state.wave;
      if (!ctx.state.stackupLive || !wave || !(await wave.ready())) return;
      const state = await wave.state();
      if (!state || state === ctx.state.stackupSnapshot) return;
      ctx.state.stackupSnapshot = state;
      ctx.state.saved.surfer = state;
      ctx.state.saved.markerNames = ctx.state.markerNames || [];
      await ctx.settingsSet(ctx.state.saved);
    },
  };

  window.gatecap.registerPanel(LOGIC_ANALYZER_UUID, impl);
})();
