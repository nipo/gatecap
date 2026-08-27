"""Unit tests for the control/status instrument driver: what it reads out of
the envelope tail, where it decides its registers are, and what its node API
and its pane do over them.

The target is an in-memory model of the register file -- the same semantics the
gateware implements (write-1-to-clear sticky bits, counters rebased rather than
zeroed, action registers reading zero) -- served by a fake bridge together with
a synthetic rack descriptor, so no gateware is involved.

Run: python3.13 -m pytest host/tests/test_control_status.py
"""

import asyncio

import cbor2
import pytest

from acrobe.node import Node

from acrobe_plugin.gatecap.enumerator import BlockAddress, MemoryMappedEnumerator
from acrobe_plugin.gatecap.instrument.control_status.blocks.registers import (
    PANEL_REGISTERS_UUID, PanelRegisters)
from acrobe_plugin.gatecap.instrument.control_status.driver import (
    CONTROL_STATUS_UUID, ControlStatusPanel)
from acrobe_plugin.gatecap.instrument.control_status.inventory import (
    PanelInventory, PanelMap)
from acrobe_plugin.gatecap.rack import RACK_UUID

DESCRIPTOR_BASE = 0x8000
PANEL_BASE = 0x1000
FINGERPRINT = 0xC0FFEE01

# The panel of the design document: three controls (one of them enum-bound),
# two statuses (one of them enum-bound), two tick-out words and one tick-in
# word, counters four bits wide.
CONTROL_SPEC = "led,dac_level[0:11],mode[0:1]<idle,run,test>"
STATUS_SPEC = "state[0:3]<reset,idle,busy>,done"
TICK_OUT = ["start,stop", "soft_reset"]
TICK_IN = ["overflow,underflow"]
COUNTER_WIDTH = 4


class Panel:
    """The register file, as the gateware implements it: word offsets from the
    block base, in the layout the register-map convention fixes."""

    WORD = 4

    def __init__(self, controls, statuses, tick_out, tick_in, counter_width):
        self.control_widths = controls
        self.status_widths = statuses
        self.tick_out_widths = tick_out
        self.tick_in_widths = tick_in
        self.counter_width = counter_width
        self.controls = [0] * len(controls)
        self.statuses = [0] * len(statuses)
        self.sticky = [0] * len(tick_in)
        self.counters = [0] * sum(tick_in)
        self.bases = [0] * sum(tick_in)
        self.strobes = []          # (word, mask) per tick-out write, in order

    # -- layout ------------------------------------------------------------

    @property
    def sticky_clear(self):
        return len(self.tick_out_widths)

    @property
    def counter_clear(self):
        return self.sticky_clear + len(self.tick_in_widths)

    @property
    def status_words(self):
        return 0x200 // self.WORD

    def counter_index(self, word, bit):
        return sum(self.tick_in_widths[:word]) + bit

    # -- the events the panel would see ------------------------------------

    def event(self, word, bit, times=1):
        """``times`` one-cycle pulses on a tick input: the sticky bit is set
        and the counter steps, both wrapping at the declared width."""
        self.sticky[word] |= 1 << bit
        index = self.counter_index(word, bit)
        self.counters[index] = (self.counters[index] + times) \
            % (1 << self.counter_width)

    def counter_of(self, index):
        return (self.counters[index] - self.bases[index]) \
            % (1 << self.counter_width)

    # -- register access ---------------------------------------------------

    def read(self, offset):
        word = offset // self.WORD
        if offset < 0x200:
            return 0                              # action: write-only
        if 0x200 <= offset < 0x300:
            index = word - self.status_words
            if index == 0:
                return 0                          # STATUS
            if index == 1:
                return FINGERPRINT
            index -= 2
            if index < len(self.sticky):
                return self.sticky[index]
            index -= len(self.sticky)
            if index < len(self.statuses):
                return self.statuses[index]
            index -= len(self.statuses)
            return self.counter_of(index)
        return self.controls[word - 0x300 // self.WORD]

    def write(self, offset, value):
        word = offset // self.WORD
        if offset < 0x100:
            if word < self.sticky_clear:
                self.strobes.append((word, value
                                     & ((1 << self.tick_out_widths[word]) - 1)))
            elif word < self.counter_clear:
                self.sticky[word - self.sticky_clear] &= ~value
            else:
                group = word - self.counter_clear
                for bit in range(self.tick_in_widths[group]):
                    if (value >> bit) & 1:
                        index = self.counter_index(group, bit)
                        self.bases[index] = self.counters[index]
            return
        if offset >= 0x300:
            index = word - 0x300 // self.WORD
            self.controls[index] = value & ((1 << self.control_widths[index]) - 1)
            return
        raise AssertionError(f"write to a read-only register at {offset:#x}")


class FakeBridge(Node):
    """The descriptor blob at the enumerator's base, the panel model at the
    panel's."""

    word_bytes = 4

    def __init__(self, blob, panel, panel_base):
        super().__init__("bridge")
        self.blob = blob
        self.panel = panel
        self.panel_base = panel_base

    async def mem_read(self, addr, size):
        if addr == DESCRIPTOR_BASE:
            return self.blob[:size].ljust(size, b"\x00")
        raw = b""
        for index in range(size // self.word_bytes):
            value = await self.read32(addr + index * self.word_bytes)
            raw += value.to_bytes(self.word_bytes, "little")
        return raw

    async def read32(self, addr):
        assert addr >= self.panel_base, f"read outside the panel at {addr:#x}"
        return self.panel.read(addr - self.panel_base)

    async def write32(self, addr, value):
        assert addr >= self.panel_base, f"write outside the panel at {addr:#x}"
        self.panel.write(addr - self.panel_base, value)

    async def mem_write(self, addr, data):
        for index in range(0, len(data), self.word_bytes):
            await self.write32(
                addr + index,
                int.from_bytes(data[index:index + self.word_bytes], "little"))


def envelope(name="panel", control=CONTROL_SPEC, status=STATUS_SPEC,
             tick_out=None, tick_in=None, counter_width=COUNTER_WIDTH):
    return [CONTROL_STATUS_UUID, 10, name,
            {"registers": [0, [PANEL_REGISTERS_UUID]]},
            control, status,
            TICK_OUT if tick_out is None else tick_out,
            TICK_IN if tick_in is None else tick_in,
            counter_width]


def target(**kwargs):
    """Enumerate a one-panel rack over a fresh model; return (panel node,
    model, root). The model is dimensioned from the very tail the descriptor
    carries, so a scenario states its panel once."""
    async def run():
        described = envelope(**kwargs)
        inventory = PanelInventory.parse(described[4:], described[2])
        panel = Panel(controls=[f.width for f in inventory.controls],
                      statuses=[f.width for f in inventory.statuses],
                      tick_out=[len(word) for word in inventory.tick_out],
                      tick_in=[len(word) for word in inventory.tick_in],
                      counter_width=inventory.counter_width)
        blob = cbor2.dumps([RACK_UUID, 0, {PANEL_BASE: described}])
        bridge = FakeBridge(blob, panel, DESCRIPTOR_BASE + PANEL_BASE)
        enumerator = MemoryMappedEnumerator(bridge, DESCRIPTOR_BASE,
                                            "enumerator")
        bridge.child_add(enumerator)
        await enumerator.start()
        node, = [child for child in bridge.children
                 if isinstance(child, ControlStatusPanel)]
        return node, panel, bridge
    return asyncio.run(run())


class Session:
    """One enumerated panel, its model, and a place to run coroutines against
    both -- every step of a scenario on one event loop."""

    def __init__(self, **kwargs):
        self.node, self.panel, self.root = target(**kwargs)

    def do(self, *coroutines):
        async def run():
            return [await coroutine for coroutine in coroutines]
        return asyncio.run(run())

    def one(self, coroutine):
        return self.do(coroutine)[0]


# -- the descriptor tail ---------------------------------------------------

def test_the_tail_is_the_whole_inventory():
    inventory = PanelInventory.parse(envelope()[4:], "panel")
    assert [(f.name, f.width) for f in inventory.controls] == [
        ("led", 1), ("dac_level", 12), ("mode", 2)]
    assert [(f.name, f.width) for f in inventory.statuses] == [
        ("state", 4), ("done", 1)]
    assert inventory.controls[2].enum == {0: "idle", 1: "run", 2: "test"}
    assert inventory.statuses[0].enum == {0: "reset", 1: "idle", 2: "busy"}
    assert inventory.controls[0].enum is None
    # Word boundaries are the simultaneity groups, so they survive; tick
    # inputs are numbered word-major, which is their counter order.
    assert [[t.name for t in word] for word in inventory.tick_out] == [
        ["start", "stop"], ["soft_reset"]]
    assert [(t.name, t.word, t.bit, t.index)
            for t in inventory.tick_in_ticks()] == [
        ("overflow", 0, 0, 0), ("underflow", 0, 1, 1)]
    assert inventory.counter_width == COUNTER_WIDTH


def test_an_absent_kind_is_an_empty_field():
    inventory = PanelInventory.parse(
        envelope(control="gate", status="", tick_out=[], tick_in=[])[4:],
        "mini")
    assert [f.name for f in inventory.controls] == ["gate"]
    assert inventory.statuses == () and inventory.tick_out == ()
    assert inventory.tick_in == () and inventory.counter_count() == 0


def test_a_malformed_tail_is_refused():
    with pytest.raises(ValueError, match="tail field"):
        PanelInventory.parse([CONTROL_SPEC, STATUS_SPEC, [], []], "panel")
    with pytest.raises(ValueError, match="counter width"):
        PanelInventory.parse([CONTROL_SPEC, STATUS_SPEC, [], [], 33], "panel")
    with pytest.raises(ValueError, match="element 0 was expected"):
        # Descending, so element 0 would be the high bit: a panel names its
        # registers ascending, and anything else is a different value.
        PanelInventory.parse(["level[11:0]", "", [], [], 4], "panel")


def test_the_map_follows_the_register_convention():
    # The very offsets the gateware bench drives, derived from the inventory
    # alone: no copy of the map is in the descriptor.
    layout = PanelMap(PanelInventory.parse(envelope()[4:], "panel"))
    assert (layout.tick_out(0), layout.tick_out(1)) == (0x000, 0x004)
    assert layout.sticky_clear(0) == 0x008
    assert layout.counter_clear(0) == 0x00C
    assert layout.fingerprint() == 0x204
    assert layout.sticky(0) == 0x208
    assert (layout.status(0), layout.status(1)) == (0x20C, 0x210)
    assert (layout.counter(0), layout.counter(1)) == (0x214, 0x218)
    assert [layout.control(i) for i in range(3)] == [0x300, 0x304, 0x308]
    assert layout.status_run == 7    # STATUS, FINGERPRINT, sticky, 2, 2


# -- enumeration -----------------------------------------------------------

def test_the_panel_enumerates_with_its_register_file():
    node, _, root = target()
    assert node.name == "panel" and node.size == 1024
    assert node.base == DESCRIPTOR_BASE + PANEL_BASE
    registers, = node.children
    assert isinstance(registers, PanelRegisters)
    assert registers.base == node.base and registers.name == "registers"
    # Addressed through its instrument, and by its bare name while it is
    # unambiguous.
    targets = BlockAddress.targets(root.children_find(lambda x: True))
    assert targets["panel"] is node
    assert targets["panel.registers"] is registers
    assert targets["registers"] is registers
    # The instrument's panel speaks for the register file, which is the whole
    # instrument, so the block offers no UI of its own.
    assert not hasattr(registers, "ui_adaptor")


# -- controls --------------------------------------------------------------

def test_controls_are_written_and_read_back():
    s = Session()
    s.do(s.node.control_write("led", 1),
         s.node.control_write("dac_level", 0xABC),
         s.node.control_write("mode", "test"))
    assert s.panel.controls == [1, 0xABC, 2]
    values = s.one(s.node.controls_read())
    assert values == {"led": 1, "dac_level": 0xABC, "mode": 2}
    assert s.node.label("mode", values["mode"]) == "test"
    assert s.one(s.node.control_read("dac_level")) == 0xABC


def test_a_control_refuses_what_it_cannot_hold():
    s = Session()
    with pytest.raises(ValueError, match="does not fit the 12-bit"):
        s.one(s.node.control_write("dac_level", 1 << 12))
    with pytest.raises(ValueError, match="not a label of 'mode'"):
        s.one(s.node.control_write("mode", "spin"))
    with pytest.raises(ValueError, match="carries no enumeration"):
        s.one(s.node.control_write("led", "on"))
    with pytest.raises(KeyError, match="no control named 'nope'"):
        s.one(s.node.control_write("nope", 1))
    assert s.panel.controls == [0, 0, 0]


# -- statuses --------------------------------------------------------------

def test_statuses_are_read_and_decoded():
    s = Session()
    s.panel.statuses = [2, 1]
    assert s.one(s.node.status_read("state")) == 2
    assert s.node.label("state", 2) == "busy"
    assert s.node.label("state", 7) is None
    assert s.one(s.node.status_read("done")) == 1


# -- tick outputs ----------------------------------------------------------

def test_ticks_of_one_word_strobe_in_one_write():
    s = Session()
    s.one(s.node.strobe("start", "stop"))
    s.one(s.node.strobe("soft_reset"))
    assert s.panel.strobes == [(0, 0b11), (1, 0b1)]


def test_ticks_of_two_words_are_not_simultaneous_and_say_so():
    s = Session()
    with pytest.raises(ValueError, match="cannot fire in one cycle"):
        s.one(s.node.strobe("start", "soft_reset"))
    assert s.panel.strobes == []
    # Naming them separately is what makes the sequence explicit.
    assert s.one(s.node.strobe_each("start", "soft_reset")) == 2
    assert s.panel.strobes == [(0, 0b1), (1, 0b1)]


# -- tick inputs -----------------------------------------------------------

def test_sticky_clearing_touches_only_the_named_bits():
    s = Session()
    s.panel.event(0, 0)
    s.panel.event(0, 1)
    assert s.one(s.node.sticky_read()) == {"overflow": True, "underflow": True}
    s.one(s.node.sticky_clear("overflow"))
    assert s.one(s.node.sticky_read()) == {"overflow": False,
                                           "underflow": True}


def test_counters_wrap_and_are_rebased_by_a_clear():
    s = Session()
    s.panel.event(0, 0, times=3)
    s.panel.event(0, 1, times=1)
    assert s.one(s.node.counters_read()) == {"overflow": 3, "underflow": 1}
    s.one(s.node.counters_clear("overflow"))
    assert s.one(s.node.counters_read()) == {"overflow": 0, "underflow": 1}
    # Four-bit counters, so they wrap where the gateware wraps.
    s.panel.event(0, 0, times=20)
    assert s.one(s.node.counters_read())["overflow"] == 20 % 2 ** COUNTER_WIDTH


def test_reset_clears_the_sticky_bit_and_the_counter_together():
    s = Session()
    s.panel.event(0, 1, times=5)
    s.one(s.node.reset("underflow"))
    assert s.one(s.node.sticky_read())["underflow"] is False
    assert s.one(s.node.counters_read())["underflow"] == 0


# -- the poll --------------------------------------------------------------

def test_a_poll_reads_the_whole_live_panel_in_one_burst():
    s = Session()
    s.panel.statuses = [3, 1]
    s.panel.event(0, 1, times=2)
    poll = s.one(s.node.poll())
    assert poll["fingerprint"] == FINGERPRINT
    assert poll["status"] == {"state": 3, "done": 1}
    assert poll["sticky"] == {"overflow": False, "underflow": True}
    assert poll["counters"] == {"overflow": 0, "underflow": 2}
    # A tick input nobody has acknowledged is what a panel reports.
    assert (poll["state"], poll["tone"]) == ("event", "attention")
    assert poll["progress"] == "underflow"
    s.one(s.node.sticky_clear("underflow"))
    quiet = s.one(s.node.poll())
    assert (quiet["state"], quiet["tone"], quiet["progress"]) == ("idle",
                                                                 "idle", "")
    # Controls are never polled: nothing in the poll reads the array region.
    assert s.one(s.node.fingerprint()) == FINGERPRINT


# -- the frontends ---------------------------------------------------------

def test_the_console_describes_the_inventory():
    s = Session()
    lines = s.node.ui_adaptor("console").info()
    assert "  control dac_level: 12 bit(s)" in lines
    assert "  control mode: 2 bit(s) <0=idle, 1=run, 2=test>" in lines
    assert "  status done: 1 bit(s)" in lines
    assert "  tick out word 0: start, stop" in lines
    assert "  tick in word 0: overflow, underflow" in lines
    assert "  counters: 2, 4 bit(s), wrapping" in lines


def test_the_pane_describes_itself_and_routes_its_ops():
    s = Session()
    gui = s.node.ui_adaptor("gui", None)
    meta = gui.describe()
    assert meta["name"] == "panel" and meta["type"] == str(CONTROL_STATUS_UUID)
    assert meta["controls"] == [
        {"name": "led", "width": 1, "enum": None},
        {"name": "dac_level", "width": 12, "enum": None},
        {"name": "mode", "width": 2,
         "enum": {"0": "idle", "1": "run", "2": "test"}}]
    assert [f["width"] for f in meta["statuses"]] == [4, 1]
    assert meta["tick_out"] == [["start", "stop"], ["soft_reset"]]
    assert meta["tick_in"] == [["overflow", "underflow"]]
    assert meta["counter_width"] == COUNTER_WIDTH and meta["key"]

    s.one(gui.message({"op": "control", "name": "mode", "value": 1}))
    assert s.one(gui.message({"op": "controls"}))["values"]["mode"] == 1
    s.one(gui.message({"op": "tick", "names": ["start", "stop"]}))
    assert s.panel.strobes == [(0, 0b11)]
    s.panel.event(0, 0)
    s.one(gui.message({"op": "ack", "names": ["overflow"]}))
    assert s.one(s.node.sticky_read())["overflow"] is False
    s.panel.event(0, 0, times=4)
    s.one(gui.message({"op": "reset", "names": ["overflow"]}))
    assert s.one(s.node.counters_read())["overflow"] == 0
    with pytest.raises(ValueError, match="unknown op"):
        s.one(gui.message({"op": "arm"}))


def test_the_pane_serves_its_own_panel_js():
    # The pane ships its UI as a package resource, minted under the block's own
    # URL namespace -- the shell loads one panel.js per type UUID.
    from acrobe_plugin.gatecap.gui.resources import ResourceServer

    s = Session()
    gui = s.node.ui_adaptor("gui", ResourceServer())
    assert gui.panel_url().startswith("/r/")
    body, ctype = gui.resource("panel.js")
    assert ctype == "text/javascript"
    assert str(CONTROL_STATUS_UUID).encode() in body
    assert b"registerPanel" in body
    assert gui.resource("trace.vcd") is None


def test_a_panel_of_one_kind_still_works():
    # The degenerate shapes: no status, no tick at all. Nothing about the map
    # or the API depends on a kind being present.
    s = Session(control="gate", status="", tick_out=[], tick_in=[])
    s.one(s.node.control_write("gate", True))
    assert s.panel.controls == [1]
    poll = s.one(s.node.poll())
    assert poll["sticky"] == {} and poll["counters"] == {}
    assert poll["status"] == {} and poll["state"] == "idle"
    assert s.one(s.node.sticky_read()) == {}
    assert s.one(s.node.counters_read()) == {}
