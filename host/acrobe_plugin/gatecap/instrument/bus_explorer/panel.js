// Bus-explorer panel: one pane for the whole instrument, in four rows.
//
//   raw access    address / data / mask entry, read and write buttons, and the
//                 last result -- the mode that works with no map at all.
//   registers     the scan slots, which is what "registers of interest" means
//                 in hardware: address, the name the map gives it, the value
//                 the scanner last read, and its valid / error flags. Adding a
//                 register programs a slot; the values arrive on the status
//                 poll, so the table costs the transport nothing.
//   fields        the field breakdown of the selected mapped register, each
//                 field editable -- editing one computes its mask and sends a
//                 masked write, which the gateware performs as a
//                 read-modify-write on the target.
//   journal       every write of the session, exportable as a listing or as a
//                 recipe the node replays.
//
// With no map loaded the register column stays empty and the field row says
// so: raw hex is a working mode, not a degraded one.
(function () {
  const BUS_EXPLORER_UUID = "5804305e-b62b-400f-94e3-86c905d87b97";

  // The pane styles itself: an instrument ships its own presentation, and the
  // shell knows nothing of panels. Injected once, whatever the pane count.
  const STYLE = `
  .bx-row { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap;
            padding: .3rem .7rem; border-bottom: 1px solid #2c2c2c; }
  .bx-row > .bx-title { color: #7fd1ff; font-size: .78rem; min-width: 5.5rem; }
  .bx-cell { display: flex; align-items: center; gap: .3rem;
             border: 1px solid #3a3a3a; border-radius: 4px; padding: .1rem .35rem; }
  .bx-cell > label { font: 11px/1.4 monospace; color: #cdd; white-space: nowrap; }
  .bx-cell.based > label { cursor: pointer; text-decoration: underline dotted #789; }
  .bx-cell input { width: 7rem; font-family: monospace; }
  .bx-cell input.bad { color: #ff7a7a; }
  .bx-out { font: 11px/1.4 monospace; color: #9fe; }
  .bx-out.bad { color: #ff7a7a; }
  .bx-table { width: 100%; border-collapse: collapse; font: 11px/1.5 monospace; }
  .bx-table th { text-align: left; color: #7fd1ff; font-weight: normal;
                 border-bottom: 1px solid #2c2c2c; padding: .15rem .4rem; }
  .bx-table td { padding: .1rem .4rem; border-bottom: 1px solid #222; }
  .bx-table tr.sel td { background: #16323f; }
  .bx-table tr.err td { color: #ff7a7a; }
  .bx-table td.name { color: #cdd; cursor: pointer; }
  .bx-table td.val { color: #9fe; }
  .bx-flag { color: #666; }
  .bx-flag.on { color: #7fdc7f; }
  .bx-flag.bad { color: #ff7a7a; }
  .bx-journal { max-height: 11rem; overflow: auto; margin: 0; padding: .3rem .7rem;
                font: 11px/1.4 monospace; color: #cdd; white-space: pre; }
  .bx-scroll { max-height: 13rem; overflow: auto; }
  .bx-note { font: 11px/1.4 monospace; color: #998; }
  `;

  const ensureCss = () => {
    if (document.getElementById("bx-style")) return;
    const s = document.createElement("style");
    s.id = "bx-style";
    s.textContent = STYLE;
    document.head.appendChild(s);
  };

  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const hex = (value, digits) =>
    "0x" + (value >>> 0).toString(16).padStart(digits, "0");
  const bin = (value, width) => value.toString(2).padStart(width, "0");
  // The entry's value in the base its cell shows, or null when it is not a
  // number of that base or does not fit the target's width.
  const parse = (input, width, base) => {
    const text = input.value.trim();
    const ok = base === "bin" ? /^[01]+$/.test(text)
                              : /^(0x)?[0-9a-f]+$/i.test(text);
    const value = ok ? parseInt(text.replace(/^0x/i, ""), base === "bin" ? 2 : 16)
                     : NaN;
    const bad = !ok || !(value >= 0) || value > 2 ** width - 1;
    input.classList.toggle("bad", bad);
    return bad ? null : value;
  };

  const impl = {
    async render(ctx, el, block) {
      ensureCss();
      const saved = await ctx.settingsGet();
      ctx.state.el = el;
      ctx.state.block = block;
      ctx.state.base = saved.base || "hex";
      ctx.state.selected = null;    // address whose fields the field row shows
      ctx.state.map = { loaded: false, registers: [] };
      ctx.state.addressDigits = Math.ceil(block.address_width / 4);
      ctx.state.valueDigits = Math.ceil(block.data_width / 4);
      el.innerHTML =
        `<div class="pane-controls">`
        + `<span><span class="name">${esc(block.name)}</span>`
        + `<span class="kind">bus explorer</span></span>`
        + `<span class="bx-note">${block.address_width} address bit(s), `
        + `${block.data_width} data bit(s), ${block.slot_count} slot(s)`
        + (block.map_id ? `, map ${esc(block.map_id)}` : "") + `</span>`
        + `</div>`
        + this.accessRow(ctx, block)
        + this.slotsRow()
        + `<div class="bx-row"><span class="bx-title">fields</span>`
        + `<span class="bx-note" data-bx="fields">select a register</span></div>`
        + this.journalRow();
      this.wire(ctx, el, block);
      await this.loadMap(ctx);
      await this.refreshSlots(ctx);
    },

    // -- raw access --------------------------------------------------------

    accessRow(ctx, block) {
      const entry = (key, label, title) =>
        `<span class="bx-cell based" data-base="${ctx.state.base}">`
        + `<label data-bx-base="${key}" title="${title}">${label}</label>`
        + `<input data-bx="${key}" value=""></span>`;
      return `<div class="bx-row"><span class="bx-title">access</span>`
        + entry("address", "address",
                "target address, driven onto paddr verbatim")
        + entry("data", "data", "write data; a read ignores it")
        + entry("mask", "mask",
                "bits to write; empty writes the whole word, anything else "
                + "is a read-modify-write done by the engine")
        + `<button data-bx="read">read</button>`
        + `<button data-bx="write">write</button>`
        + `<span class="bx-out" data-bx="result">-</span></div>`;
    },

    // -- registers of interest --------------------------------------------

    slotsRow() {
      return `<div class="bx-row"><span class="bx-title">registers</span>`
        + `<button data-bx="slot-add" title="program a scan slot with the `
        + `address in the access row">add address</button>`
        + `<label class="bx-note"><input type="checkbox" data-bx="scan"> `
        + `scan</label>`
        + `<span class="bx-note" data-bx="scan-note"></span></div>`
        + `<div class="bx-scroll"><table class="bx-table">`
        + `<thead><tr><th>#</th><th>address</th><th>register</th>`
        + `<th>value</th><th>flags</th><th></th></tr></thead>`
        + `<tbody data-bx="slots"></tbody></table></div>`;
    },

    journalRow() {
      return `<div class="bx-row"><span class="bx-title">journal</span>`
        + `<button data-bx="journal" title="the plain listing of every write `
        + `this session made">listing</button>`
        + `<button data-bx="recipe" title="the same writes as a recipe this `
        + `pane can replay">recipe</button>`
        + `<button data-bx="replay" title="execute the recipe shown below `
        + `against this target">replay</button>`
        + `<button data-bx="clear">clear</button>`
        + `<span class="bx-out" data-bx="journal-note">-</span></div>`
        + `<pre class="bx-journal" data-bx="journal-text"></pre>`;
    },

    // -- wiring -----------------------------------------------------------

    wire(ctx, el, block) {
      const at = (key) => el.querySelector(`[data-bx="${key}"]`);
      at("read").onclick = () => this.doRead(ctx);
      at("write").onclick = () => this.doWrite(ctx);
      at("slot-add").onclick = () => this.addSlot(ctx);
      at("scan").onchange = (e) => this.setScan(ctx, e.target.checked);
      at("journal").onclick = () => this.showJournal(ctx);
      at("recipe").onclick = () => this.showRecipe(ctx);
      at("replay").onclick = () => this.replay(ctx);
      at("clear").onclick = () => this.clearJournal(ctx);
      // Double-click any entry's name to switch the whole access row between
      // hex and binary: one target, one base.
      el.querySelectorAll("[data-bx-base]").forEach((label) => {
        label.ondblclick = () => this.toggleBase(ctx);
      });
    },

    async toggleBase(ctx) {
      const el = ctx.state.el;
      const base = ctx.state.base === "bin" ? "hex" : "bin";
      const widths = { address: ctx.state.block.address_width,
                       data: ctx.state.block.data_width,
                       mask: ctx.state.block.data_width };
      Object.keys(widths).forEach((key) => {
        const input = el.querySelector(`[data-bx="${key}"]`);
        const value = input.value.trim()
          ? parse(input, widths[key], ctx.state.base) : null;
        input.closest(".bx-cell").dataset.base = base;
        if (value !== null)
          input.value = base === "bin" ? bin(value, widths[key])
                                       : hex(value, 0);
        input.classList.remove("bad");
      });
      ctx.state.base = base;
      await ctx.settingsSet({ base });
    },

    field(ctx, key, width, required) {
      const input = ctx.state.el.querySelector(`[data-bx="${key}"]`);
      if (!input.value.trim()) {
        input.classList.toggle("bad", !!required);
        return required ? undefined : null;
      }
      const value = parse(input, width, ctx.state.base);
      return value === null ? undefined : value;
    },

    result(ctx, text, bad) {
      const out = ctx.state.el.querySelector(`[data-bx="result"]`);
      out.textContent = text;
      out.classList.toggle("bad", !!bad);
    },

    async doRead(ctx) {
      const address = this.field(ctx, "address", ctx.state.block.address_width,
                                 true);
      if (address === undefined) return this.result(ctx, "bad address", true);
      const r = await ctx.send({ op: "read", address });
      if (r.error) return this.result(ctx, r.error, true);
      ctx.state.lastRead = r;
      this.result(ctx, `${hex(address, ctx.state.addressDigits)} -> `
        + `${hex(r.value, ctx.state.valueDigits)}`
        + (r.register ? `  ${r.register}` : ""));
      this.showFields(ctx, address, r.value, r.fields || []);
    },

    async doWrite(ctx) {
      const block = ctx.state.block;
      const address = this.field(ctx, "address", block.address_width, true);
      const value = this.field(ctx, "data", block.data_width, true);
      const mask = this.field(ctx, "mask", block.data_width, false);
      if (address === undefined || value === undefined || mask === undefined)
        return this.result(ctx, "bad entry", true);
      const msg = { op: "write", address, value };
      if (mask !== null) msg.mask = mask;
      const r = await ctx.send(msg);
      if (r.error) return this.result(ctx, r.error, true);
      this.result(ctx, r.summary);
      ctx.log(block.name + ": " + r.summary);
      await this.showJournal(ctx);
    },

    // -- the map and the field row ----------------------------------------

    async loadMap(ctx) {
      const r = await ctx.send({ op: "map" });
      if (r.error && !r.loaded) ctx.log(ctx.state.block.name + ": " + r.error);
      ctx.state.map = r.loaded ? r : { loaded: false, registers: [] };
    },

    registerAt(ctx, address) {
      return (ctx.state.map.registers || []).find(r => r.address === address);
    },

    showFields(ctx, address, value, fields) {
      const holder = ctx.state.el.querySelector(`[data-bx="fields"]`);
      ctx.state.selected = address;
      const register = this.registerAt(ctx, address);
      if (!register || !fields.length) {
        holder.textContent = register
          ? `${register.name}: no fields in the map`
          : (ctx.state.map.loaded ? `no register at `
              + `${hex(address, ctx.state.addressDigits)} in the map`
              : "no register map loaded: raw hex");
        holder.className = "bx-note";
        return;
      }
      holder.className = "";
      holder.innerHTML = `<span class="bx-note">${esc(register.name)}</span> `
        + fields.map((f, i) => this.fieldWidget(register, f, i)).join("");
      fields.forEach((f, i) => {
        const node = holder.querySelector(`[data-bx-field="${i}"]`);
        if (!node) return;
        node.onchange = () => this.writeField(ctx, register, f, node.value);
      });
    },

    fieldWidget(register, f, index) {
      const label = `<label title="[${f.msb}:${f.lsb}], ${f.width} bit(s)`
        + (f.description ? " -- " + esc(f.description) : "") + `">`
        + `${esc(f.name)}</label>`;
      const writable = register.writable
        && (!f.access || String(f.access).indexOf("write") >= 0);
      const enums = Object.keys(f.enum || {});
      if (enums.length) {
        const opts = enums.sort((a, b) => a - b).map(
          v => `<option value="${v}"${+v === f.value ? " selected" : ""}>`
            + `${esc(f.enum[v])}</option>`).join("");
        return `<span class="bx-cell">${label}<select data-bx-field="${index}"`
          + `${writable ? "" : " disabled"}>${opts}</select></span>`;
      }
      return `<span class="bx-cell">${label}`
        + `<input data-bx-field="${index}" value="${hex(f.value, 0)}"`
        + `${writable ? "" : " disabled"}></span>`;
    },

    async writeField(ctx, register, f, text) {
      const value = /^(0x)?[0-9a-f]+$/i.test(String(text).trim())
        ? parseInt(String(text).replace(/^0x/i, ""), 16) : NaN;
      if (!(value >= 0)) return this.result(ctx, `bad ${f.name} value`, true);
      const r = await ctx.send({ op: "field", register: register.name,
                                 field: f.name, value });
      if (r.error) return this.result(ctx, r.error, true);
      this.result(ctx, r.summary);
      ctx.log(ctx.state.block.name + ": " + r.summary);
      await this.showJournal(ctx);
    },

    // -- the slots ---------------------------------------------------------

    async refreshSlots(ctx) {
      const r = await ctx.send({ op: "slots" });
      if (r.error) { ctx.log("slot read failed: " + r.error); return; }
      ctx.state.el.querySelector(`[data-bx="scan"]`).checked = !!r.scan;
      this.drawSlots(ctx, r.slots || []);
    },

    drawSlots(ctx, slots) {
      const body = ctx.state.el.querySelector(`[data-bx="slots"]`);
      if (!body) return;
      body.innerHTML = slots.map((slot) => {
        const address = slot.address == null ? "-"
          : hex(slot.address, ctx.state.addressDigits);
        const register = slot.register
          || (slot.address == null ? ""
              : (this.registerAt(ctx, slot.address) || {}).name || "");
        const value = slot.valid ? hex(slot.value, ctx.state.valueDigits) : "-";
        return `<tr data-bx-slot="${slot.index}"`
          + `${slot.error ? ' class="err"' : ""}>`
          + `<td>${slot.index}</td><td class="name">${address}</td>`
          + `<td class="name">${esc(register)}</td>`
          + `<td class="val">${value}</td>`
          + `<td><span class="bx-flag${slot.valid ? " on" : ""}">valid</span> `
          + `<span class="bx-flag${slot.error ? " bad" : ""}">err</span></td>`
          + `<td><label><input type="checkbox" data-bx-slot-en="${slot.index}"`
          + `${slot.enabled ? " checked" : ""}> on</label></td></tr>`;
      }).join("");
      body.querySelectorAll("[data-bx-slot-en]").forEach((box) => {
        const index = +box.dataset.bxSlotEn;
        box.onchange = async () => {
          const r = await ctx.send({ op: "slot_enable", index,
                                     enabled: box.checked });
          if (r.error) ctx.log("slot enable failed: " + r.error);
        };
      });
      // Clicking a row shows that register's fields, read fresh.
      body.querySelectorAll("[data-bx-slot]").forEach((row) => {
        const slot = slots[+row.dataset.bxSlot];
        row.onclick = async () => {
          if (slot.address == null) return;
          body.querySelectorAll("tr").forEach(r => r.classList.remove("sel"));
          row.classList.add("sel");
          const r = await ctx.send({ op: "read", address: slot.address });
          if (r.error) return this.result(ctx, r.error, true);
          this.showFields(ctx, slot.address, r.value, r.fields || []);
        };
      });
    },

    async addSlot(ctx) {
      const address = this.field(ctx, "address", ctx.state.block.address_width,
                                 true);
      if (address === undefined) return this.result(ctx, "bad address", true);
      const slots = ctx.state.slots || [];
      // The first slot with no address of its own, else the first at all: a
      // slot is a scarce resource and the pane does not silently grow one.
      let index = slots.findIndex(s => !s.enabled);
      if (index < 0) index = 0;
      const r = await ctx.send({ op: "slot_set", index, address });
      if (r.error) return this.result(ctx, r.error, true);
      this.result(ctx, r.summary);
      await this.refreshSlots(ctx);
    },

    async setScan(ctx, enabled) {
      const r = await ctx.send({ op: "scan", enabled });
      if (r.error) ctx.log("scan control failed: " + r.error);
      else ctx.log(ctx.state.block.name + ": " + r.summary);
    },

    // -- the journal -------------------------------------------------------

    async showJournal(ctx) {
      const r = await ctx.send({ op: "journal" });
      if (r.error) return;
      const el = ctx.state.el;
      el.querySelector(`[data-bx="journal-text"]`).textContent = r.text;
      el.querySelector(`[data-bx="journal-note"]`).textContent =
        `${r.entries.length} write(s)`;
      ctx.state.journal = r.entries;
    },

    async showRecipe(ctx) {
      const r = await ctx.send({ op: "recipe" });
      if (r.error) return;
      ctx.state.el.querySelector(`[data-bx="journal-text"]`).textContent =
        r.text;
      ctx.state.recipe = r.recipe;
    },

    async replay(ctx) {
      const recipe = ctx.state.recipe;
      if (!recipe) { ctx.log("show a recipe first"); return; }
      const r = await ctx.send({ op: "replay", recipe });
      if (r.error) ctx.log("replay failed: " + r.error);
      else ctx.log(ctx.state.block.name + `: replayed ${r.steps} step(s)`);
      await this.showJournal(ctx);
    },

    async clearJournal(ctx) {
      await ctx.send({ op: "journal_clear" });
      await this.showJournal(ctx);
    },

    // Every poll carries the engine's state and every slot: the table is
    // painted from it, so the registers of interest are live without the pane
    // reading anything of its own.
    onStatus(ctx, block, status) {
      const root = ctx.state.el;
      if (!root) return;
      ctx.state.slots = status.scan || [];
      this.drawSlots(ctx, ctx.state.slots);
      const note = root.querySelector(`[data-bx="scan-note"]`);
      if (note) note.textContent = status.progress || "";
      const box = root.querySelector(`[data-bx="scan"]`);
      if (box) box.checked = !!status.scan_active;
    },
  };

  window.gatecap.registerPanel(BUS_EXPLORER_UUID, impl);
})();
