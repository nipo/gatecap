"""Control/status instrument driver: a front panel of plain wires.

The instrument binds on the envelope UUID, reads its whole inventory out of
the envelope tail (:mod:`.inventory`) and drives it through its one child, the
register file. It is the only node of the panel a caller talks to: signals are
addressed by the names the description gave them, whatever kind they are and
whatever register they live in.

What the four kinds cost on the wire follows from their semantics:

* a control is written when the user acts on it and read back once, to
  synchronise a widget with a value only the hardware still holds -- never
  polled;
* everything live -- the sticky bits, the status levels and the counters --
  is one contiguous read-only run, so a status poll reads the whole panel in
  one burst;
* a sticky bit is write-1-to-clear: a clear names the bits it has seen, and
  an event landing on one between the read and the clear is kept;
* a counter wraps at the width the panel declares and is rebased, not zeroed
  (the gateware counts on, the register subtracts a base), so a clear loses no
  event that was already in flight.
"""

import uuid
from importlib.resources import files

from acrobe_plugin.gatecap.enumerator import (MemoryMappedEnumerator,
                                              MemoryMappedInstrument)
from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor, GuiAdaptor

from .blocks.registers import PanelRegisters
from .inventory import PanelInventory, PanelMap

# Must match CONTROL_STATUS_UUID_C in the gateware (gatecap.descriptor).
CONTROL_STATUS_UUID = uuid.UUID("dd241b36-f1b0-4418-b6b8-23223e5a93ff")


@MemoryMappedEnumerator.instruments.register(CONTROL_STATUS_UUID)
class ControlStatusPanel(MemoryMappedInstrument):
    # The one child of the envelope, at offset 0.
    BLOCK = "registers"
    # A panel is idle until an event it has not acknowledged is pending; the
    # tone raises that to the user's attention, which is the whole point of a
    # sticky bit.
    STATE_IDLE = "idle"
    STATE_EVENT = "event"

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        self.inventory = PanelInventory.parse(self.tail, envelope.name)
        self.map = PanelMap(self.inventory)
        # The last fingerprint read, so a caller that has one need not re-read
        # it to answer a poll.
        self.last_fingerprint = None
        self.__registers = None

    def siblings_resolve(self, siblings):
        """Bind the register file. Called by the enumerator once every child of
        the instrument exists."""
        child = siblings.get(self.BLOCK)
        if child is None:
            raise LookupError(
                f"control/status panel {self.name!r} has no {self.BLOCK!r} "
                f"child (children: {', '.join(sorted(siblings))})")
        if not isinstance(child, PanelRegisters):
            raise TypeError(
                f"control/status panel {self.name!r} holds {self.BLOCK!r} as a "
                f"{type(child).__name__}, not its register file")
        self.__registers = child

    @property
    def registers(self):
        assert self.__registers is not None, (
            f"control/status panel {self.name!r} was never resolved against "
            f"its children")
        return self.__registers

    # -- controls ----------------------------------------------------------

    async def control_write(self, name, value):
        """Write one control. ``value`` is an integer, or a label of the
        control's own enumeration table."""
        field = self.inventory.control(name)
        await self.registers.write(self.map.control(field.index),
                                   field.encode(value))

    async def control_read(self, name):
        """One control, read back from the register that holds it."""
        field = self.inventory.control(name)
        value = await self.registers.word(self.map.control(field.index))
        return value & field.mask

    async def controls_read(self):
        """Every control, in one burst of the array region: what a frontend
        synchronises its widgets from when it attaches to a running target."""
        if not self.inventory.controls:
            return {}
        words = await self.registers.words(self.map.ARRAY,
                                           self.map.control_words)
        return {field.name: words[field.index] & field.mask
                for field in self.inventory.controls}

    # -- statuses ----------------------------------------------------------

    async def status_read(self, name):
        field = self.inventory.status(name)
        value = await self.registers.word(self.map.status(field.index))
        return value & field.mask

    def label(self, name, value):
        """The label a control or status value decodes to, or None when the
        signal carries no enumeration or nothing binds the value."""
        for kind in (self.inventory.controls, self.inventory.statuses):
            for field in kind:
                if field.name == name:
                    return field.decode(value)
        raise KeyError(f"no control or status named {name!r}")

    # -- tick outputs ------------------------------------------------------

    async def strobe(self, *names):
        """Fire tick outputs, all of them in the same instrument-clock cycle.

        Simultaneity is the packed word: every named tick must sit in the same
        one, because one write is what makes them fire together. A set spread
        over several words cannot be fired simultaneously at all, and is
        refused here rather than turned into a sequence that looks like one --
        see :meth:`strobe_each` for that."""
        ticks = [self.inventory.tick_output(name) for name in names]
        if not ticks:
            raise ValueError(
                f"strobing panel {self.name!r} names no tick output")
        words = {tick.word for tick in ticks}
        if len(words) != 1:
            groups = "; ".join(
                f"word {word}: "
                + ", ".join(tick.name for tick in ticks if tick.word == word)
                for word in sorted(words))
            raise ValueError(
                f"tick outputs {', '.join(names)} of panel {self.name!r} are "
                f"spread over {len(words)} packed words ({groups}), which "
                f"cannot fire in one cycle: one write fires one word. Strobe "
                f"each word on its own, or use strobe_each() to say plainly "
                f"that they are not simultaneous")
        mask = 0
        for tick in ticks:
            mask |= 1 << tick.bit
        await self.registers.write(self.map.tick_out(ticks[0].word), mask)

    async def strobe_each(self, *names):
        """Fire tick outputs spread over several words: one write per word, in
        word order. The ticks of a word are simultaneous; the words are not --
        that is what naming them separately means. Returns the number of
        writes it took."""
        ticks = [self.inventory.tick_output(name) for name in names]
        words = sorted({tick.word for tick in ticks})
        for word in words:
            await self.strobe(*[tick.name for tick in ticks
                                if tick.word == word])
        return len(words)

    # -- tick inputs -------------------------------------------------------

    async def sticky_read(self):
        """``{tick name: pending}`` over the tick inputs."""
        if not self.inventory.tick_in:
            return {}
        words = await self.registers.words(
            self.map.sticky(0), self.map.tick_in_words)
        return self.__sticky(words, 0)

    async def sticky_clear(self, *names):
        """Clear the named sticky bits -- exactly those, so an event that
        arrived on another one after it was read is not lost. Clearing is one
        write per packed word."""
        for word, mask in self.__masks(names, self.inventory.tick_input):
            await self.registers.write(self.map.sticky_clear(word), mask)

    async def counters_read(self):
        """``{tick name: count}`` over the tick inputs, each already rebased
        by its last clear and wrapping at the panel's counter width."""
        if not self.inventory.tick_in:
            return {}
        words = await self.registers.words(self.map.counter(0),
                                           self.map.counter_words)
        return {tick.name: words[tick.index]
                for tick in self.inventory.tick_in_ticks()}

    async def counters_clear(self, *names):
        """Rebase the named counters, so they read from zero again."""
        for word, mask in self.__masks(names, self.inventory.tick_input):
            await self.registers.write(self.map.counter_clear(word), mask)

    async def reset(self, *names):
        """Acknowledge the named tick inputs whole: clear their sticky bits and
        rebase their counters."""
        await self.sticky_clear(*names)
        await self.counters_clear(*names)

    def __masks(self, names, lookup):
        """``(word, mask)`` per packed word the named ticks touch."""
        masks = {}
        for name in names:
            tick = lookup(name)
            masks[tick.word] = masks.get(tick.word, 0) | (1 << tick.bit)
        return sorted(masks.items())

    def __sticky(self, words, offset):
        return {tick.name: bool((words[offset + tick.word] >> tick.bit) & 1)
                for tick in self.inventory.tick_in_ticks()}

    # -- the whole live state ----------------------------------------------

    async def fingerprint(self):
        """Per-instance descriptor UID, the same one every block of the rack
        reports."""
        self.last_fingerprint = await self.registers.word(
            self.map.fingerprint())
        return self.last_fingerprint

    async def snapshot(self):
        """Everything the panel shows, in one burst read of the status region:
        the fingerprint, the sticky bits, the status levels and the counters.
        The controls are not in it -- they are host state, written and read
        back on demand."""
        words = await self.registers.words(self.map.STATUS,
                                           self.map.status_run)
        self.last_fingerprint = words[1]
        return {"fingerprint": words[1],
                "sticky": self.__sticky(words, self.map.sticky_at),
                "status": {field.name: words[self.map.status_at + field.index]
                                       & field.mask
                           for field in self.inventory.statuses},
                "counters": {tick.name: words[self.map.counter_at + tick.index]
                             for tick in self.inventory.tick_in_ticks()}}

    async def poll(self):
        """This instrument's status for a frontend: the whole live panel, plus
        the state and tone its pill is drawn from. A tick input with an
        unacknowledged event is what a panel has to report."""
        snapshot = await self.snapshot()
        pending = [name for name, set_ in snapshot["sticky"].items() if set_]
        return dict(snapshot,
                    state=self.STATE_EVENT if pending else self.STATE_IDLE,
                    tone="attention" if pending else "idle",
                    progress=", ".join(pending))

    # -- frontends ---------------------------------------------------------

    def ui_adaptor(self, frontend, resources=None):
        cached = self.__dict__.get(f"ui_{frontend}")
        if cached is not None:
            return cached
        if frontend == "gui":
            adaptor = PanelGui(self, resources)
        elif frontend == "console":
            adaptor = PanelConsole(self)
        else:
            return None
        self.__dict__[f"ui_{frontend}"] = adaptor
        return adaptor


class PanelConsole(ConsoleAdaptor):
    """Console UI for a panel: its inventory, kind by kind."""

    def info(self):
        driver = self.driver
        inventory = driver.inventory
        lines = [f"{driver.name}:", "  control/status panel"]
        for kind, fields in (("control", inventory.controls),
                             ("status", inventory.statuses)):
            for field in fields:
                enum = (" <" + ", ".join(
                    f"{value}={label}"
                    for value, label in sorted(field.enum.items())) + ">"
                    if field.enum else "")
                lines.append(f"  {kind} {field.name}: {field.width} bit(s)"
                             + enum)
        for kind, words in (("tick out", inventory.tick_out),
                            ("tick in", inventory.tick_in)):
            for index, word in enumerate(words):
                names = ", ".join(tick.name for tick in word)
                lines.append(f"  {kind} word {index}: {names}")
        if inventory.tick_in:
            lines.append(f"  counters: {inventory.counter_count()}, "
                         f"{inventory.counter_width} bit(s), wrapping")
        return lines


class PanelGui(GuiAdaptor):
    """Web UI for a panel: the widgets its inventory calls for, and the ops
    behind them. Every widget is a pure function of kind, width and enum
    binding, so the pane is built from the descriptor alone."""

    PANEL = files(__package__).joinpath("panel.js")
    ORDER = 50   # below the capture panels, which own the waveform surfaces

    def describe(self):
        inventory = self.driver.inventory
        meta = {"name": self.address(), "type": str(CONTROL_STATUS_UUID),
                "controls": [self.field(f) for f in inventory.controls],
                "statuses": [self.field(f) for f in inventory.statuses],
                "tick_out": [[tick.name for tick in word]
                             for word in inventory.tick_out],
                "tick_in": [[tick.name for tick in word]
                            for word in inventory.tick_in],
                "counter_width": inventory.counter_width}
        meta["key"] = self.panel_key(meta)
        return meta

    @staticmethod
    def field(field):
        # Enum keys go out as text: they are JSON object keys on the pane's
        # side, and the pane indexes the table with the value it renders.
        return {"name": field.name, "width": field.width,
                "enum": (None if field.enum is None
                         else {str(value): label
                               for value, label in field.enum.items()})}

    async def message(self, msg):
        op, driver = msg.get("op"), self.driver
        if op == "controls":
            return {"values": await driver.controls_read()}
        if op == "control":
            name, value = msg["name"], msg["value"]
            await driver.control_write(name, value)
            return {"ok": True, "summary": f"{name}={value}"}
        if op == "tick":
            names = list(msg.get("names", []))
            await driver.strobe(*names)
            return {"ok": True, "summary": "strobe " + ", ".join(names)}
        if op == "ack":
            names = list(msg.get("names", []))
            await driver.sticky_clear(*names)
            return {"ok": True}
        if op == "reset":
            names = list(msg.get("names", []))
            await driver.reset(*names)
            return {"ok": True, "summary": "reset " + ", ".join(names)}
        raise ValueError(f"unknown op {op!r}")
