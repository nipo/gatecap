"""Unit tests for rack enumeration: instrument nodes, their children, and the
scoping of the references between them.

The descriptors here are synthetic CBOR blobs served by a fake bridge, so no
gateware is involved.

Run: python3.13 -m pytest host/tests/test_rack_enum.py
"""

import asyncio
import uuid

import cbor2
import pytest

from acrobe.node import Node

from acrobe_plugin.gatecap.instrument.la.blocks.buffer import BUFFER_UUID, Buffer
from acrobe_plugin.gatecap.instrument.la.blocks.control import CONTROL_UUID, Control
from acrobe_plugin.gatecap.instrument.la.blocks.trigger import TRIGGER_UUID, Trigger
from acrobe_plugin.gatecap.enumerator import (MemoryMappedEnumerator,
                                              MemoryMappedInstrument,
                                              UnknownInstrument)
from acrobe_plugin.gatecap.rack import RACK_UUID

COUNTER_UUID = uuid.UUID("778010a3-b4ff-4156-aa92-2eaca97ecfe4")
ANALYZER_UUID = uuid.UUID("0786abe7-3599-4074-b0d7-8c727847a08d")
STRANGER_UUID = uuid.UUID("f493c40e-934c-4f2d-a54d-4c0deeaafc67")

DESCRIPTOR_BASE = 0x8000


class Counter(MemoryMappedInstrument):
    """A childless instrument: everything it publishes is in the tail."""

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        self.reference_hz, self.clock_names = self.tail
        self.scope = None

    def siblings_resolve(self, children):
        self.scope = children


class Analyzer(MemoryMappedInstrument):
    """An instrument whose children are the register files it drives."""

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        self.controls = None

    def siblings_resolve(self, children):
        self.controls = [child for child in children.values()
                         if isinstance(child, Control)]


@pytest.fixture(autouse=True)
def registered():
    MemoryMappedEnumerator.instruments.register(COUNTER_UUID)(Counter)
    MemoryMappedEnumerator.instruments.register(ANALYZER_UUID)(Analyzer)
    yield
    del MemoryMappedEnumerator.instruments.registry[COUNTER_UUID]
    del MemoryMappedEnumerator.instruments.registry[ANALYZER_UUID]


class FakeBridge(Node):
    """Enough of the bridge for enumeration: a word size and a read that
    serves one descriptor blob from the enumerator's base."""

    word_bytes = 4

    def __init__(self, blob):
        super().__init__("bridge")
        self.blob = blob

    async def mem_read(self, addr, size):
        assert addr == DESCRIPTOR_BASE, \
            "the fake bridge holds the descriptor at the advertised base"
        return self.blob[:size].ljust(size, b"\x00")


class Rack:
    """Builders for the synthetic rack descriptor."""

    @staticmethod
    def blob(segments):
        # [rack-uuid, next-offset, {base: envelope}]
        return cbor2.dumps([RACK_UUID, 0, segments])

    @staticmethod
    def envelope(type_uuid, size_l2, name, children=None, tail=()):
        return [type_uuid, size_l2, name, children or {}, *tail]

    @classmethod
    def analyzer(cls, name, type_uuid=ANALYZER_UUID):
        return cls.envelope(type_uuid, 14, name, {
            "buffer": [0x0000, [BUFFER_UUID, 32, 8]],
            "trigger": [0x1000, [TRIGGER_UUID, 2, "a,b"]],
            "control": [0x2000, [CONTROL_UUID, "buffer", "trigger", 2, "a,b",
                                 255, 1, 100_000_000, 0]],
            })

    @classmethod
    def counter(cls, name, type_uuid=COUNTER_UUID):
        return cls.envelope(type_uuid, 12, name,
                            tail=[48_000_000, ["sys", "phy"]])


def enumerate_rack(segments):
    """Run one enumeration over a synthetic rack; return the node whose
    children are the instruments."""
    async def run():
        bridge = FakeBridge(Rack.blob(segments))
        enumerator = MemoryMappedEnumerator(bridge, DESCRIPTOR_BASE,
                                            "enumerator")
        bridge.child_add(enumerator)
        await enumerator.start()
        return bridge
    return asyncio.run(run())


def node(root, name):
    match, = root.children_find(lambda x: x.name == name)
    return match


def test_one_node_per_instrument_bound_by_type():
    root = enumerate_rack({0x4000: Rack.analyzer("la"),
                           0x1000: Rack.counter("clkmon")})
    la, clkmon = node(root, "la"), node(root, "clkmon")
    assert isinstance(la, Analyzer) and isinstance(clkmon, Counter)
    assert (la.base, clkmon.base) == (DESCRIPTOR_BASE + 0x4000,
                                      DESCRIPTOR_BASE + 0x1000)
    assert (la.size, clkmon.size) == (2**14, 2**12)


def test_the_tail_reaches_the_instrument_driver():
    clkmon = node(enumerate_rack({0x1000: Rack.counter("clkmon")}), "clkmon")
    assert clkmon.tail == [48_000_000, ["sys", "phy"]]
    assert clkmon.reference_hz == 48_000_000
    assert clkmon.clock_names == ["sys", "phy"]
    # A childless instrument still gets its (empty) scope.
    assert clkmon.scope == {}


def test_children_hang_under_their_instrument_at_stacked_bases():
    root = enumerate_rack({0x4000: Rack.analyzer("la")})
    la = node(root, "la")
    assert [child.name for child in la.children] == ["buffer", "trigger",
                                                     "control"]
    buffer, trigger, control = la.children
    assert isinstance(buffer, Buffer) and isinstance(trigger, Trigger)
    # descriptor base, then the instrument's segment, then the child's offset.
    assert buffer.base == DESCRIPTOR_BASE + 0x4000
    assert trigger.base == DESCRIPTOR_BASE + 0x4000 + 0x1000
    assert control.base == DESCRIPTOR_BASE + 0x4000 + 0x2000


def test_references_resolve_inside_one_instrument():
    root = enumerate_rack({0x4000: Rack.analyzer("first"),
                           0x8000: Rack.analyzer("second")})
    first, second = node(root, "first"), node(root, "second")
    for instrument in (first, second):
        buffer, trigger, control = instrument.children
        # The names are identical in both instruments, and each control binds
        # the blocks of its own.
        assert control.sink_node_get() is buffer
        assert control.trigger_node_get() is trigger


def test_the_instrument_is_resolved_with_its_children():
    la = node(enumerate_rack({0x4000: Rack.analyzer("la")}), "la")
    assert [control.name for control in la.controls] == ["control"]


def test_an_unknown_instrument_still_enumerates():
    root = enumerate_rack({0x4000: Rack.analyzer("la", STRANGER_UUID)})
    la, = [child for child in root.children
           if isinstance(child, UnknownInstrument)]
    assert la.name == f"la (unknown {STRANGER_UUID})"
    assert la.envelope.name == "la" and la.size == 2**14
    # A known block under an unknown instrument still gets its driver.
    assert [type(child) for child in la.children] == [Buffer, Trigger, Control]
