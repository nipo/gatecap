"""Unit tests for rack enumeration: the two-level walk, reference-only
children, domain-prefixed child names, and the logic-analyzer instrument
driver.

The descriptors here are synthetic CBOR blobs served by a fake bridge, so no
gateware is involved.

Run: python3.13 -m pytest host/tests/test_descriptor_enum.py
"""

import asyncio
import uuid

import cbor2
import pytest

from acrobe.node import Node

from acrobe_plugin.gatecap.instrument.la.blocks.buffer import BUFFER_UUID, Buffer
from acrobe_plugin.gatecap.instrument.la.blocks.control import CONTROL_UUID, Control
from acrobe_plugin.gatecap.instrument.la.driver import (LOGIC_ANALYZER_UUID,
                                                         LogicAnalyzer)
from acrobe_plugin.gatecap.instrument.la.blocks.trigger import TRIGGER_UUID, Trigger
from acrobe_plugin.gatecap.enumerator import (BlockAddress,
                                              MemoryMappedEnumerator)
from acrobe_plugin.gatecap.rack import RACK_UUID, DescriptorError

# An instrument type no driver claims, for the parts of the walk that must
# work whatever sits in the envelope.
OTHER_UUID = uuid.UUID("0786abe7-3599-4074-b0d7-8c727847a08d")
# The analyzer's segment: what the backplane allocates above the 4 KB ROM.
SEGMENT = 0x4000


class FakeBridge(Node):
    """Enough of the bridge for enumeration: a word size and a read that
    serves one descriptor blob from the enumerator's base."""

    word_bytes = 4

    def __init__(self, blob):
        super().__init__("bridge")
        self.blob = blob

    async def mem_read(self, addr, size):
        assert addr == 0, "the fake bridge holds the descriptor at 0"
        return self.blob[:size].ljust(size, b"\x00")


class Descriptor:
    """Builders for the synthetic descriptor objects."""

    @staticmethod
    def root(*instruments):
        # [rack-type, next-offset, {base: envelope}]
        return cbor2.dumps([RACK_UUID, 0, dict(instruments)])

    @staticmethod
    def analyzer(children, controls, name="la", size_l2=14, base=SEGMENT,
                 type_uuid=LOGIC_ANALYZER_UUID):
        # [type, size_l2, name, children, [ member control names ]]
        return base, [type_uuid, size_l2, name, children, list(controls)]

    @staticmethod
    def buffer(offset=0x1000, sample_stride=32, size_l2=8):
        return [offset, [BUFFER_UUID, sample_stride, size_l2]]

    @staticmethod
    def trigger(offset=0x2000, signal_count=2, names="a,b"):
        return [offset, [TRIGGER_UUID, signal_count, names]]

    @staticmethod
    def control(sink, trigger, offset=0x3000, names="a,b", signal_count=2,
                clock_hz=100_000_000, integration_latency=0):
        return [offset, [CONTROL_UUID, sink, trigger, signal_count, names,
                         255, 1, clock_hz, integration_latency]]

    @classmethod
    def domain(cls, prefix, offset, clock_hz, integration_latency=0):
        """One domain's three blocks, child names prefixed with `prefix`."""
        return {
            f"{prefix}.buffer": cls.buffer(offset),
            f"{prefix}.trigger": cls.trigger(offset + 0x1000),
            f"{prefix}.control": cls.control(
                f"{prefix}.buffer", f"{prefix}.trigger", offset + 0x2000,
                clock_hz=clock_hz, integration_latency=integration_latency),
        }


def enumerate_rack(blob):
    """Run one enumeration over a synthetic descriptor; return the node whose
    descendants are the enumerated instruments and blocks."""
    async def run():
        bridge = FakeBridge(blob)
        enumerator = MemoryMappedEnumerator(bridge, 0, "enumerator")
        bridge.child_add(enumerator)
        await enumerator.start()
        return bridge
    return asyncio.run(run())


def enumerate_analyzer(children, controls, **kwargs):
    return enumerate_rack(Descriptor.root(
        Descriptor.analyzer(children, controls, **kwargs)))


def block(root, name):
    match, = root.children_find(lambda x: x.name == name)
    return match


# -- the root ---------------------------------------------------------------

def test_unknown_root_type_is_refused():
    blob = cbor2.dumps([OTHER_UUID, 0, {}])
    with pytest.raises(DescriptorError, match="not a gatecap rack"):
        enumerate_rack(blob)


def test_instrument_children_hang_under_their_instrument():
    root = enumerate_analyzer(Descriptor.domain("ctrl", 0x0, 25_000_000),
                              ["ctrl.control"])
    analyzer = block(root, "la")
    assert isinstance(analyzer, LogicAnalyzer)
    assert analyzer.base == SEGMENT
    assert analyzer.size == 1 << 14
    assert {child.name for child in analyzer.children} == {
        "ctrl.buffer", "ctrl.trigger", "ctrl.control"}
    # Child bases stack: the descriptor base, the segment, the child offset.
    assert block(root, "ctrl.control").base == SEGMENT + 0x2000


def test_an_unknown_instrument_still_enumerates_its_children():
    root = enumerate_rack(Descriptor.root(Descriptor.analyzer(
        Descriptor.domain("ctrl", 0x0, 25_000_000), ["ctrl.control"],
        name="thing", type_uuid=OTHER_UUID)))
    control = block(root, "ctrl.control")
    assert isinstance(control, Control)
    assert control.parent.name.startswith("thing (unknown ")


def test_blocks_are_addressed_through_their_instrument():
    root = enumerate_analyzer(Descriptor.domain("ctrl", 0x0, 25_000_000),
                              ["ctrl.control"])
    control = block(root, "ctrl.control")
    assert BlockAddress.of(control) == "la.ctrl.control"
    assert BlockAddress.of(block(root, "la")) == "la"
    # A bare child name is a legal address as long as one node answers to it.
    targets = BlockAddress.targets(root.children_find(lambda x: True))
    assert targets["la.ctrl.control"] is control
    assert targets["ctrl.control"] is control


# -- reference-only children ------------------------------------------------

def test_baseless_child_builds_a_driver_without_base():
    children = dict(Descriptor.domain("ctrl", 0x0, 25_000_000))
    children["note"] = [None, [OTHER_UUID, 1]]
    root = enumerate_analyzer(children, ["ctrl.control"])
    node, = root.children_find(lambda x: x.name.startswith("note "))
    assert node.base is None


def test_baseless_child_rejected_by_a_register_driver():
    # A control drives registers; a descriptor handing it no base is a
    # gateware/generator bug, and must not enumerate a half-working block.
    children = Descriptor.domain("ctrl", 0x0, 25_000_000)
    children["ctrl.control"][0] = None
    with pytest.raises(ValueError, match="no register base"):
        enumerate_analyzer(children, ["ctrl.control"])


# -- domain-prefixed child names --------------------------------------------

def test_prefixed_children_enumerate_and_resolve():
    root = enumerate_analyzer(
        {**Descriptor.domain("ctrl", 0x0, 25_000_000),
         **Descriptor.domain("rx", 0x10000, 125_000_000,
                             integration_latency=2)},
        ["ctrl.control", "rx.control"])
    names = [n.name for n in root.children_find(lambda x: True)]
    assert set(names) >= {"ctrl.buffer", "ctrl.trigger", "ctrl.control",
                          "rx.buffer", "rx.trigger", "rx.control"}

    ctrl, rx = block(root, "ctrl.control"), block(root, "rx.control")
    # Each control resolves its own domain's blocks, not the other's.
    assert ctrl.sink_node_get() is block(root, "ctrl.buffer")
    assert ctrl.trigger_node_get() is block(root, "ctrl.trigger")
    assert rx.sink_node_get() is block(root, "rx.buffer")
    assert rx.trigger_node_get() is block(root, "rx.trigger")
    assert isinstance(rx.sink_node_get(), Buffer)
    assert isinstance(rx.trigger_node_get(), Trigger)
    assert (ctrl.sample_rate, rx.sample_rate) == (25_000_000, 125_000_000)
    assert (ctrl.integration_latency, rx.integration_latency) == (0, 2)


def test_prefixed_block_name_does_not_reach_the_signal_names():
    # A dot in a block name is instance naming; a dot in a probe name is a VCD
    # scope. The two never mix: the control's probes stay as the descriptor
    # spells them.
    root = enumerate_analyzer({
        "rx.buffer": Descriptor.buffer(0x1000),
        "rx.trigger": Descriptor.trigger(0x2000),
        "rx.control": Descriptor.control("rx.buffer", "rx.trigger", 0x3000,
                                         names="word[1:0]", signal_count=2),
    }, ["rx.control"])
    control = block(root, "rx.control")
    assert control.signal_names == ["word[1]", "word[0]"]


def test_unresolvable_child_reference_is_loud():
    root = enumerate_analyzer({
        "rx.buffer": Descriptor.buffer(0x1000),
        "rx.trigger": Descriptor.trigger(0x2000),
        "rx.control": Descriptor.control("buffer", "rx.trigger", 0x3000),
    }, ["rx.control"])
    with pytest.raises(LookupError, match="sibling reference 'buffer'"):
        block(root, "rx.control").sink_node_get()


# -- the logic-analyzer instrument ------------------------------------------

def two_domains(**kwargs):
    return enumerate_analyzer(
        {**Descriptor.domain("ctrl", 0x0, 25_000_000),
         **Descriptor.domain("rx", 0x10000, 125_000_000,
                             integration_latency=2)},
        ["ctrl.control", "rx.control"], **kwargs)


def test_analyzer_resolves_its_members():
    root = two_domains()
    group = block(root, "la")
    assert group.control_names == ["ctrl.control", "rx.control"]
    assert [c.name for c in group.child_controls] == ["ctrl.control",
                                                      "rx.control"]
    assert all(isinstance(c, Control) for c in group.child_controls)
    assert group.child_controls[1] is block(root, "rx.control")


def test_analyzer_unknown_member_fails_enumeration():
    with pytest.raises(LookupError, match="tx.control"):
        enumerate_analyzer(Descriptor.domain("ctrl", 0x0, 25_000_000),
                           ["ctrl.control", "tx.control"])


def test_analyzer_member_must_be_a_control():
    with pytest.raises(TypeError, match="not a capture control"):
        enumerate_analyzer(Descriptor.domain("ctrl", 0x0, 25_000_000),
                           ["ctrl.buffer"])


def test_analyzer_without_members_is_rejected():
    with pytest.raises(ValueError, match="references no control block"):
        enumerate_analyzer(Descriptor.domain("ctrl", 0x0, 25_000_000), [])


def test_a_single_domain_analyzer_is_a_group_of_one():
    # One domain is a group of one: it has nothing to be correlated with, so
    # it is captured in its member's own samples. The analyzer is still the
    # instrument's one panel, and its blocks offer none.
    root = enumerate_analyzer(Descriptor.domain("ctrl", 0x0, 25_000_000),
                              ["ctrl.control"])
    analyzer = block(root, "la")
    assert not analyzer.grouped()
    # The console says nothing the member's own info lines do not.
    assert analyzer.ui_adaptor("console") is None
    meta = analyzer.ui_adaptor("gui", None).describe()
    assert meta["name"] == "la" and meta["grouped"] is False
    assert [m["name"] for m in meta["members"]] == ["ctrl.control"]
    assert [t["name"] for t in meta["triggers"]] == ["ctrl.trigger"]
    for name in ("ctrl.control", "ctrl.trigger", "ctrl.buffer"):
        child = block(root, name)
        assert getattr(child, "ui_adaptor", lambda *a: None)("gui", None) is None


def test_an_analyzer_publishes_every_trigger_it_holds():
    # The editors a panel renders are those of the trigger blocks the
    # instrument contains, whatever their flavour, each addressed by name.
    root = two_domains()
    analyzer = block(root, "la")
    meta = analyzer.ui_adaptor("gui", None).describe()
    assert [t["name"] for t in meta["triggers"]] == ["ctrl.trigger",
                                                     "rx.trigger"]
    assert {t["kind"] for t in meta["triggers"]} == {"value"}
    assert [f["name"] for f in meta["triggers"][0]["fields"]] == ["a", "b"]
    assert analyzer.trigger_by_name("rx.trigger") is block(root, "rx.trigger")
    with pytest.raises(KeyError, match="holds no trigger"):
        analyzer.trigger_by_name("nope")


def test_analyzer_console_info():
    lines = block(two_domains(), "la").ui_adaptor("console").info()
    assert lines[0] == "la:"
    assert "2 control(s)" in lines[1]
    assert any("ctrl.control" in l and "25 MHz" in l for l in lines)
    assert any("rx.control" in l and "125 MHz" in l and "latency 2" in l
               for l in lines)


def correlated_group():
    """An analyzer whose second domain subscribes to the first domain's
    trigger -- the topology a group stands for."""
    return enumerate_analyzer({
        **Descriptor.domain("ctrl", 0x0, 25_000_000),
        "rx.buffer": Descriptor.buffer(0x10000),
        "rx.control": Descriptor.control("rx.buffer", "ctrl.trigger", 0x12000,
                                         clock_hz=125_000_000,
                                         integration_latency=2),
    }, ["ctrl.control", "rx.control"])


def test_analyzer_resolves_the_trigger_its_members_share():
    root = correlated_group()
    group = block(root, "la")
    assert group.trigger_node_get() is block(root, "ctrl.trigger")
    # Total back-dating per member: the trigger's own pipeline, plus the
    # crossing the descriptor reports for that member.
    assert [c.trigger_latency() for c in group.child_controls] == [
        Trigger.LATENCY, Trigger.LATENCY + 2]


def test_analyzer_refuses_members_on_different_triggers():
    # Two self-triggering domains are two independent events, not a group.
    with pytest.raises(ValueError, match="not a correlated group"):
        block(two_domains(), "la").trigger_node_get()


def test_analyzer_derives_each_members_window_from_one_duration():
    # 25 MHz and 125 MHz: the same 400 ns window, five times as many samples
    # on one member as on the other -- the point of stating it in time.
    group = block(correlated_group(), "la")
    plan = group.plan(seconds=400e-9, pre_seconds=80e-9)
    assert plan.params("ctrl.control") == {"count": 10, "pretrigger": 2,
                                           "windows": 1}
    assert plan.params("rx.control") == {"count": 50, "pretrigger": 10,
                                         "windows": 1}
    assert plan.notes == []
    # A per-member override replaces the derivation for that member alone.
    plan = group.plan(seconds=400e-9, overrides={"rx.control": {"count": 16}})
    assert plan.params("rx.control")["count"] == 16
    assert plan.params("ctrl.control")["count"] == 10
    with pytest.raises(ValueError, match="no group member"):
        group.plan(seconds=400e-9, overrides={"nope.control": {}})


def test_analyzer_clamps_a_window_its_buffer_cannot_hold():
    group = block(correlated_group(), "la")
    # 2 µs is 250 samples at 125 MHz, and that member's buffer holds 64.
    plan = group.plan(seconds=2e-6)
    assert plan.params("ctrl.control")["count"] == 50
    assert plan.params("rx.control")["count"] == 64
    assert len(plan.notes) == 1 and "capturing 64" in plan.notes[0]
