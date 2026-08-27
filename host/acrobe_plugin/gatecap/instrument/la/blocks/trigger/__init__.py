"""Trigger block driver: a value/mask compare over the block's own signal
vector, driving a capture core's trigger line. A control block references it
by name; configure it, then arm the control block.

The block offers no surface of its own: its editor is a section of the panel
of the analyzer that holds it, which is also where the capture it fires is
armed. What the driver publishes for that editor is ``describe()`` (the fields
to render) and ``apply()`` (the compare to write) -- what the analyzer's
adaptor calls, whichever flavour of trigger it holds.
"""

import uuid

from acrobe_plugin.gatecap.enumerator import (MemoryMappedBlock,
                                             MemoryMappedEnumerator)
from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor

from .....names import SignalNames
from ...signals import VcdLayout

# Must match the UUIDs in the gateware (gatecap.descriptor).
TRIGGER_UUID = uuid.UUID("2a7c4e19-8b53-4f0a-9d61-7e2c5b048f36")
EDGE_TRIGGER_UUID = uuid.UUID("9f4e2c17-6a3b-4d8e-b1c5-7e0a2f6d3b94")


@MemoryMappedEnumerator.db.register(TRIGGER_UUID)
class Trigger(MemoryMappedBlock):
    # What an editor of this flavour offers: a level match per field.
    KIND = "value"
    # Config group at 0x100 (gatecap map convention); VALUE, MASK contiguous.
    REG_VALUE = 0x100
    REG_MASK = 0x104
    # Cycles the trigger strobe trails the matched condition (must match the
    # gateware trigger_control_latency_c). The RLE trace marker is skewed back
    # by this; the raw core back-dates in hardware.
    LATENCY = 1

    def __init__(self, bridge, base, name, obj):
        super().__init__(bridge, base, name)
        # [type, signal-count, signal-names]. signal-names is a grouping
        # spec over the trigger's own inputs (disjoint from the probes).
        _, self.signal_count, names = obj
        self.signal_names, self.signal_enums = SignalNames.parse(names)

    async def configure(self, value, mask):
        # VALUE and MASK are contiguous -> one burst write.
        wb = self.bridge.word_bytes
        await self.bridge.mem_write(
            self.base + self.REG_VALUE,
            value.to_bytes(wb, "little") + mask.to_bytes(wb, "little"))

    def describe(self):
        """What a frontend needs to render this trigger's editor: its flavour,
        its own signals and the fields they group into. Descriptor data only --
        no hardware is read."""
        return {"name": self.name, "kind": self.KIND,
                "signal_count": self.signal_count,
                "signal_names": self.signal_names,
                "fields": trigger_fields(self.signal_names, self.signal_enums)}

    async def apply(self, params):
        """Write the compare an editor holds, and return a one-line summary of
        what went to the hardware."""
        value, mask = params.get("value", 0), params.get("mask", 0)
        await self.configure(value, mask)
        return f"value={value:#x} mask={mask:#x}"

    def ui_adaptor(self, frontend, resources=None):
        if frontend != "console":
            return None
        cached = self.__dict__.get("ui_console")
        if cached is None:
            cached = self.__dict__["ui_console"] = TriggerConsole(self)
        return cached


def trigger_fields(names, enums=None):
    """Group the flat per-bit signal names into trigger fields: a scalar signal
    is one bit (set don't-care/0/1); an array (bus[7:0]) is one field with a
    value/mask over its bits. Reuses the VCD bus grouping. Each field is
    ``{kind:"bit", name, bit, enum}`` or ``{kind:"bus", name, width, bits:[(pos,
    global-bit)], enum}``, where ``enum`` is a ``{value: label}`` map or None.
    Shared by the GUI editor and the console term parser."""
    fields = []
    for v in VcdLayout(names, enums=enums).vars:
        full = ".".join(v.scope + (v.name,))
        if v.size == 1:
            fields.append({"kind": "bit", "name": full, "enum": v.enum,
                           "bit": next(iter(v.positions.values()))})
        else:
            fields.append({"kind": "bus", "name": full, "width": v.size,
                           "enum": v.enum, "bits": sorted(v.positions.items())})
    return fields


def field_value(field, rhs):
    """Resolve a term's right-hand side to an integer: a numeric literal, or an
    enum label when the field carries a table."""
    enum = field.get("enum")
    if enum is not None:
        for value, label in enum.items():
            if label == rhs:
                return value
    return int(rhs, 0)


class TriggerConsole(ConsoleAdaptor):
    """Console UI for a trigger block: describes it and parses a filter spec."""

    def info(self):
        d = self.driver
        return [f"{d.name}:",
                f"  signals ({d.signal_count}): {', '.join(d.signal_names)}",
                "  value-mask match"]

    def parse(self, spec):
        """(value, mask) from a whole-vector "value/mask" spec (each an int like
        0xa0/0xf0); omit /mask for an exact match, "0/0" to trigger immediately."""
        if "/" in spec:
            value, mask = spec.split("/", 1)
            return int(value, 0), int(mask, 0)
        return int(spec, 0), (1 << self.driver.signal_count) - 1

    async def apply(self, terms):
        """Parse the terms and write the compare to the trigger hardware,
        returning a one-line summary for the CLI."""
        value, mask = self.parse_terms(terms)
        await self.driver.configure(value, mask)
        return f"value={value:#x} mask={mask:#x}"

    def parse_terms(self, terms):
        """(value, mask) from CLI --trigger terms. No terms triggers on any
        sample. A single term with no '=' is a whole-vector value/mask spec
        (see parse). Otherwise each term is NAME=VALUE over the trigger fields:
        a scalar takes 0/1; a bus takes a hex value (full mask on its bits) or
        VALUE/MASK. Unlisted signals stay don't-care."""
        if not terms:
            return 0, 0
        if len(terms) == 1 and "=" not in terms[0]:
            return self.parse(terms[0])
        fields = {f["name"]: f
                  for f in trigger_fields(self.driver.signal_names,
                                          self.driver.signal_enums)}
        value = mask = 0
        for term in terms:
            if "=" not in term:
                raise ValueError(f"trigger term {term!r} is not NAME=VALUE")
            name, rhs = term.split("=", 1)
            field = fields.get(name)
            if field is None:
                raise ValueError(f"unknown trigger signal {name!r} "
                                 f"(available: {', '.join(fields)})")
            if field["kind"] == "bit":
                bit_val = field_value(field, rhs)
                if bit_val not in (0, 1):
                    raise ValueError(f"bit {name!r} must be 0 or 1, not {rhs!r}")
                mask |= 1 << field["bit"]
                if bit_val:
                    value |= 1 << field["bit"]
            else:
                if "/" in rhs:
                    vs, ms = rhs.split("/", 1)
                    bv, bm = int(vs, 0), int(ms, 0)
                else:
                    bv, bm = field_value(field, rhs), (1 << field["width"]) - 1
                for pos, bit in field["bits"]:
                    if (bm >> pos) & 1:
                        mask |= 1 << bit
                        if (bv >> pos) & 1:
                            value |= 1 << bit
        return value, mask


@MemoryMappedEnumerator.db.register(EDGE_TRIGGER_UUID)
class EdgeTrigger(MemoryMappedBlock):
    """Edge/transition trigger block: an independent value/mask compare on the
    current (new) and previous-cycle (old) signal values. Configured with four
    N-wide values; a control block references it by name and just arms."""

    # What an editor of this flavour offers: a level or an edge per field.
    KIND = "edge"
    # Config group at 0x100 (gatecap map convention); the four words contiguous.
    REG_NEW_VALUE = 0x100
    REG_NEW_MASK = 0x104
    REG_OLD_VALUE = 0x108
    REG_OLD_MASK = 0x10c
    # Twice-registered inputs -> one more cycle than the value trigger (must
    # match the gateware trigger_control_edge_latency_c).
    LATENCY = 2

    def __init__(self, bridge, base, name, obj):
        super().__init__(bridge, base, name)
        # [type, signal-count, signal-names] -- same shape as the value trigger.
        _, self.signal_count, names = obj
        self.signal_names, self.signal_enums = SignalNames.parse(names)

    async def configure(self, new_value, new_mask, old_value, old_mask):
        # NEW/OLD VALUE/MASK are contiguous -> one burst write.
        wb = self.bridge.word_bytes
        data = b"".join(v.to_bytes(wb, "little") for v in
                        (new_value, new_mask, old_value, old_mask))
        await self.bridge.mem_write(self.base + self.REG_NEW_VALUE, data)

    def describe(self):
        """Same shape as the value trigger's, distinguished by ``kind``: an
        editor renders both from the fields, and offers edges on this one."""
        return {"name": self.name, "kind": self.KIND,
                "signal_count": self.signal_count,
                "signal_names": self.signal_names,
                "fields": trigger_fields(self.signal_names, self.signal_enums)}

    async def apply(self, params):
        nv, nm = params.get("new_value", 0), params.get("new_mask", 0)
        ov, om = params.get("old_value", 0), params.get("old_mask", 0)
        await self.configure(nv, nm, ov, om)
        return f"new={nv:#x}/{nm:#x} old={ov:#x}/{om:#x}"

    def ui_adaptor(self, frontend, resources=None):
        if frontend != "console":
            return None
        cached = self.__dict__.get("ui_console")
        if cached is None:
            cached = self.__dict__["ui_console"] = EdgeTriggerConsole(self)
        return cached


class EdgeTriggerConsole(ConsoleAdaptor):
    """Console UI for an edge trigger: describes it and parses edge terms."""

    def info(self):
        d = self.driver
        return [f"{d.name}:",
                f"  signals ({d.signal_count}): {', '.join(d.signal_names)}",
                "  edge/transition match (level, rising, falling)"]

    async def apply(self, terms):
        nv, nm, ov, om = self.parse_terms(terms)
        await self.driver.configure(nv, nm, ov, om)
        return f"new={nv:#x}/{nm:#x} old={ov:#x}/{om:#x}"

    def parse_terms(self, terms):
        """(new_value, new_mask, old_value, old_mask) from CLI --trigger terms.
        No terms triggers on any sample. Each term is NAME=SPEC over the trigger
        fields: a scalar takes 0/1 (level) or rising/falling (edge); a bus takes
        a hex value or VALUE/MASK (a level match on the current value). Unlisted
        signals stay don't-care."""
        fields = {f["name"]: f
                  for f in trigger_fields(self.driver.signal_names,
                                          self.driver.signal_enums)}
        nv = nm = ov = om = 0
        for term in terms:
            if "=" not in term:
                raise ValueError(f"trigger term {term!r} is not NAME=SPEC")
            name, rhs = term.split("=", 1)
            field = fields.get(name)
            if field is None:
                raise ValueError(f"unknown trigger signal {name!r} "
                                 f"(available: {', '.join(fields)})")
            if field["kind"] == "bit":
                bit = 1 << field["bit"]
                spec = rhs.strip().lower()
                if spec == "1":
                    nv |= bit; nm |= bit
                elif spec == "0":
                    nm |= bit
                elif spec in ("rising", "r", "up"):
                    nv |= bit; nm |= bit; om |= bit          # old=0, new=1
                elif spec in ("falling", "f", "down"):
                    nm |= bit; ov |= bit; om |= bit          # old=1, new=0
                elif spec == "-":
                    pass
                else:
                    raise ValueError(f"bit {name!r} takes 0/1/rising/falling, "
                                     f"not {rhs!r}")
            else:
                if "/" in rhs:
                    vs, ms = rhs.split("/", 1)
                    bv, bm = int(vs, 0), int(ms, 0)
                else:
                    bv, bm = field_value(field, rhs), (1 << field["width"]) - 1
                for pos, bit in field["bits"]:
                    if (bm >> pos) & 1:
                        nm |= 1 << bit
                        if (bv >> pos) & 1:
                            nv |= 1 << bit
        return nv, nm, ov, om
