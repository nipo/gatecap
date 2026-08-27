"""Unit tests of the logic-analyzer instrument driver: the parts that need no
hardware -- the common timebase, where each member's samples land relative to
the trigger (back-dating included), how several domains compose into one VCD,
how one window given in real time becomes each member's own parameters, and
the group's state composition.

Members are synthesised from descriptor objects, so a rate, an integration
latency, a storage kind or a probe layout can be varied without a simulator.

Run: python3.13 -m pytest host/tests/test_logic_analyzer.py
"""

import asyncio
import uuid

import pytest

from acrobe_plugin.gatecap.instrument.la.blocks.control import Control, RleControl
from acrobe_plugin.gatecap.instrument.la.driver import (LOGIC_ANALYZER_UUID,
                                                         AnalyzerConsole,
                                                         AnalyzerGui,
                                                         LogicAnalyzer)
from acrobe_plugin.gatecap.instrument.la.compose import (ComposedTrace,
                                                                 DomainTrace,
                                                                 Timebase)
from acrobe_plugin.gatecap.instrument.la.plan import (Duration,
                                                             GroupWindow)
from acrobe_plugin.gatecap.rack import Instrument as Envelope

CONTROL_UUID = uuid.UUID("bf023668-f44d-46f0-a318-03aa06223021")
RLE_CONTROL_UUID = uuid.UUID("5d3f8a21-9e74-4c60-b1d2-6f0a83e5c497")


class FakeTrigger:
    """Stands in for a trigger block: only its intrinsic latency matters to a
    control's back-dating."""

    LATENCY = 1

    def __init__(self, name="trigger", latency=1):
        self.name = name
        self.LATENCY = latency


class FakeBuffer:
    """Stands in for a trace buffer: a control's window derivation only asks
    it how deep it is."""

    def __init__(self, depth):
        self.depth = depth


class Armed:
    """Records what a synthetic member was armed with, so the parameters the
    group derived can be read back without a bridge."""

    def __init__(self):
        self.params = None

    async def configure_and_arm(self, **params):
        self.params = params


class SyntheticControl(Armed, Control):
    """A control built straight from a descriptor object, with its trigger and
    its trace buffer handed over instead of resolved through an enumerated
    sibling map, and a settable status so group composition can be exercised
    offline. Arming records its parameters instead of writing registers."""

    def __init__(self, name, *, trigger, sample_rate, integration_latency=0,
                 names="a,b[1:0]", signal_count=3, max_length=1024,
                 max_windows=4, depth=4096):
        Control.__init__(self, None, 0x1000, name,
                         [CONTROL_UUID, f"{name}.buffer", trigger.name,
                          signal_count, names, max_length, max_windows,
                          sample_rate, integration_latency])
        Armed.__init__(self)
        self.trigger_block = trigger
        self.buffer = FakeBuffer(depth)
        self.state = (self.STATE_IDLE, False, 0)

    def trigger_node_get(self):
        return self.trigger_block

    def sink_node_get(self):
        return self.buffer

    async def status(self):
        return self.state


class SyntheticRleControl(Armed, RleControl):
    """The run-length-encoded flavour of the same, so a group's dispatch on
    member type can be exercised offline."""

    def __init__(self, name, *, trigger, sample_rate, integration_latency=0,
                 names="a,b[1:0]", signal_count=3, depth=256):
        RleControl.__init__(self, None, 0x1000, name,
                            [RLE_CONTROL_UUID, f"{name}.buffer", trigger.name,
                             signal_count, names, sample_rate,
                             integration_latency])
        Armed.__init__(self)
        self.trigger_block = trigger
        self.buffer = FakeBuffer(depth)
        self.state = (self.STATE_IDLE, False)

    def trigger_node_get(self):
        return self.trigger_block

    def sink_node_get(self):
        return self.buffer

    async def status(self):
        return self.state


def group(*controls, name="la"):
    """A logic analyzer over synthetic members: the envelope names them the
    way the gateware's does, and the children they stand for are handed over
    instead of being enumerated."""
    analyzer = LogicAnalyzer(None, 0x4000, Envelope(
        name=name, type_uuid=LOGIC_ANALYZER_UUID, base=0x4000, size_l2=14,
        children={}, tail=[[c.name for c in controls]]))
    analyzer.siblings_resolve({c.name: c for c in controls})
    return analyzer


def raw_result(names, rate, windows, trigger_index, enums=None):
    return {"kind": "raw", "names": names, "signal_count": len(names),
            "sample_rate": rate, "enums": enums or {}, "windows": windows,
            "trigger_index": trigger_index}


# -- timebase ---------------------------------------------------------------

def test_timebase_is_picoseconds_for_the_usual_clocks():
    # 10 ns and 8 ns are whole picoseconds, so the coarsest resolution serves
    # both exactly -- no rounding anywhere in the composed file.
    tb = Timebase([100_000_000, 125_000_000])
    assert tb.timescale == "1 ps"
    assert tb.period(100_000_000) == 10_000
    assert tb.period(125_000_000) == 8_000


def test_timebase_goes_finer_when_a_period_is_not_whole_picoseconds():
    # 3.2 GHz is 312.5 ps: exact in hundreds of femtoseconds, not in ps.
    tb = Timebase([3_200_000_000, 100_000_000])
    assert tb.timescale == "100 fs"
    assert tb.period(3_200_000_000) == 3125
    assert tb.period(100_000_000) == 100_000


def test_timebase_rounds_only_below_a_femtosecond(caplog):
    # 3 GHz has no exact femtosecond period; the rounding is reported.
    tb = Timebase([3_000_000_000])
    assert tb.timescale == "1 fs"
    assert tb.period(3_000_000_000) == 333_333
    assert any("rounded" in r.getMessage() for r in caplog.records)


def test_timebase_refuses_an_unknown_rate():
    with pytest.raises(ValueError):
        Timebase([0])


# -- back-dating ------------------------------------------------------------

def test_trigger_latency_sums_the_intrinsic_and_integration_delays():
    # The trigger's own pipeline is a constant of its type; the wiring/CDC
    # delay to a given core is what that core's descriptor entry reports.
    trigger = FakeTrigger(latency=2)
    hosting = SyntheticControl("control.control", trigger=trigger,
                               sample_rate=100_000_000, integration_latency=0)
    subscriber = SyntheticControl("phy.control", trigger=trigger,
                                  sample_rate=125_000_000,
                                  integration_latency=3)
    assert hosting.trigger_latency() == 2
    assert subscriber.trigger_latency() == 5


def test_raw_member_places_its_trigger_at_the_pretrigger_sample():
    # A raw core back-dates its window in hardware, so the host applies no
    # further skew: sample `trigger_index` IS the trigger instant.
    trace = DomainTrace("phy.control",
                        raw_result(["w"], 125_000_000, [[0, 1, 2, 3]], 2))
    assert trace.events(0) == [(-2, 0), (-1, 1), (0, 2), (1, 3)]


def test_rle_member_back_dates_by_the_latency_it_reports():
    # An RLE member holds the strobe's trailing cycles in its pre-region, so
    # the trigger sits `trigger_latency` cycles back from the run boundary.
    result = {"kind": "rle", "names": ["w"], "sample_rate": 100_000_000,
              "enums": {}, "runs": [(0, 4), (1, 2), (0, 3)], "trigger_run": 2,
              "trigger_latency": 5}
    # Runs start at cycles 0, 4, 6; the boundary is cycle 6, the trigger 1.
    assert DomainTrace("rle.control", result).events(0) == [(-1, 0), (3, 1),
                                                            (5, 0)]


def test_a_member_without_a_sample_clock_is_refused():
    with pytest.raises(ValueError):
        DomainTrace("x", raw_result(["w"], 0, [[0]], 0))


# -- composition ------------------------------------------------------------

def test_domains_are_aligned_on_the_trigger_at_their_own_rates():
    control = raw_result(["s"], 100_000_000, [[0, 1, 0, 1, 0]], 2)
    phy = raw_result(["w"], 125_000_000, [[0, 1, 2, 3, 4]], 2)
    composed = ComposedTrace([("control.control", control), ("phy.control", phy)])

    assert composed.timebase.timescale == "1 ps"
    # The earliest sample of the capture is the file's zero (a VCD has no
    # negative time); the trigger is the marker both domains meet at.
    assert min(tick for tick, _, _ in composed.events) == 0
    assert composed.markers == [("trigger", 20_000)]

    def ticks(index):
        return [tick for tick, member, _ in composed.events if member == index]

    assert ticks(0) == [0, 10_000, 20_000, 30_000, 40_000]
    assert ticks(1) == [4_000, 12_000, 20_000, 28_000, 36_000]
    # Each domain's own sample period, exactly, and both trigger samples on
    # the one instant.
    assert ticks(0)[2] == ticks(1)[2] == composed.trigger_ticks[0]


def test_windows_follow_one_another_without_overlap():
    control = raw_result(["s"], 100_000_000, [[0, 1], [1, 0]], 1)
    phy = raw_result(["w"], 125_000_000, [[0, 1], [1, 0]], 1)
    composed = ComposedTrace([("control.control", control), ("phy.control", phy)])
    markers = dict(composed.markers)
    assert [name for name, _ in composed.markers] == ["trig0", "w1", "trig1"]
    boundary = markers["w1"]
    window0 = [tick for tick, _, _ in composed.events if tick < boundary]
    window1 = [tick for tick, _, _ in composed.events if tick >= boundary]
    # Window 1 starts a full (slowest) sample period after the last sample of
    # window 0, so the two never interleave.
    assert min(window1) == boundary == max(window0) + 10_000
    # Both domains still meet on each window's own trigger.
    for index in (0, 1):
        ticks = [tick for tick, member, _ in composed.events if member == index]
        assert ticks[1] == markers["trig0"] and ticks[3] == markers["trig1"]


def test_members_must_agree_on_the_window_count():
    control = raw_result(["s"], 100_000_000, [[0, 1], [1, 0]], 1)
    phy = raw_result(["w"], 125_000_000, [[0, 1]], 1)
    with pytest.raises(ValueError, match="windows"):
        ComposedTrace([("control.control", control), ("phy.control", phy)])


def test_vcd_carries_one_scope_per_member_and_the_enum_labels():
    control = raw_result(["state[0]", "state[1]"], 100_000_000, [[0, 3, 1]], 1,
                         enums={"state": {0: "IDLE", 1: "BUSY", 3: "DONE"}})
    phy = raw_result(["w[0]", "w[1]"], 125_000_000, [[0, 1, 2]], 1)
    vcd = ComposedTrace([("control.control", control),
                         ("phy.control", phy)]).to_vcd()[0].decode()
    assert "$timescale 1 ps $end" in vcd
    # capture > <domain> > <block>, one branch per member.
    assert vcd.count("$scope module capture $end") == 1
    assert "$scope module phy $end" in vcd and "$scope module control $end" in vcd
    assert "$var string 1" in vcd and "state" in vcd
    for label in ("sIDLE", "sDONE", "sBUSY"):
        assert label in vcd


# -- the group driver -------------------------------------------------------

def test_the_group_rejects_members_on_different_triggers():
    a = SyntheticControl("a.control", trigger=FakeTrigger("t.a"),
                         sample_rate=100_000_000)
    b = SyntheticControl("b.control", trigger=FakeTrigger("t.b"),
                         sample_rate=125_000_000)
    with pytest.raises(ValueError, match="not a correlated group"):
        group(a, b).trigger_node_get()


# -- the group window, in real time -----------------------------------------

def two_rate_group():
    """A raw group whose members run at 100 and 200 MHz -- the rates the same
    sample count would cover in half the time on one and not the other."""
    trigger = FakeTrigger()
    slow = SyntheticControl("gen.control", trigger=trigger,
                            sample_rate=100_000_000, max_length=4096,
                            depth=8192)
    fast = SyntheticControl("lb.control", trigger=trigger,
                            sample_rate=200_000_000, max_length=4096,
                            depth=8192)
    return group(slow, fast), slow, fast


def test_a_window_becomes_each_members_own_sample_counts():
    # One 10 µs window with 2 µs of pre-trigger: the same real time on both
    # members, which is twice as many samples on the 200 MHz one.
    analyzer, _, _ = two_rate_group()
    plan = analyzer.plan(seconds=10e-6, pre_seconds=2e-6)
    assert plan.params("gen.control") == {"count": 1000, "pretrigger": 200,
                                          "windows": 1}
    assert plan.params("lb.control") == {"count": 2000, "pretrigger": 400,
                                         "windows": 1}
    assert plan.notes == []
    # Both cover the window that was asked for, to the sample.
    for member in plan:
        assert member.pre_seconds == pytest.approx(2e-6)
        assert member.post_seconds == pytest.approx(8e-6)


def test_sample_counts_are_rounded_to_the_nearest_sample():
    analyzer, _, _ = two_rate_group()
    # 1.2345 µs is 123.45 samples at 100 MHz and 246.9 at 200 MHz.
    plan = analyzer.plan(seconds=1.2345e-6, pre_seconds=0.126e-6)
    assert plan.params("gen.control")["count"] == 123
    assert plan.params("lb.control")["count"] == 247
    assert plan.params("gen.control")["pretrigger"] == 13
    assert plan.params("lb.control")["pretrigger"] == 25
    assert plan.notes == []


def test_a_window_a_member_cannot_hold_is_clamped_and_said_so():
    trigger = FakeTrigger()
    small = SyntheticControl("small.control", trigger=trigger,
                             sample_rate=100_000_000, max_length=64, depth=64)
    large = SyntheticControl("large.control", trigger=trigger,
                             sample_rate=100_000_000)
    plan = group(small, large).plan(seconds=10e-6, pre_seconds=1e-6)
    assert plan.params("large.control")["count"] == 1000
    # The buffer holds 64 samples, so that is what it captures -- and the
    # pre-trigger it was asked for no longer fits either.
    assert plan.params("small.control") == {"count": 64, "pretrigger": 63,
                                            "windows": 1}
    assert len(plan.notes) == 2
    assert "1000 samples at 100 MHz, capturing 64" in plan.notes[0]
    assert "keeping 63" in plan.notes[1]
    small_plan = next(m for m in plan if m.name == "small.control")
    assert small_plan.pre_seconds + small_plan.post_seconds == pytest.approx(
        640e-9)


def test_windows_divide_the_buffer_a_member_may_use():
    trigger = FakeTrigger()
    control = SyntheticControl("a.control", trigger=trigger,
                               sample_rate=100_000_000, max_length=1024,
                               depth=1024)
    plan = group(control).plan(seconds=10e-6, windows=4)
    # 1000 samples asked for, but four windows share a 1024-sample buffer.
    assert plan.params("a.control") == {"count": 256, "pretrigger": 0,
                                        "windows": 4}
    assert "capturing 256" in plan.notes[0]


def test_a_span_shorter_than_a_sample_is_refused():
    analyzer, _, _ = two_rate_group()
    with pytest.raises(ValueError, match="shorter than one sample"):
        analyzer.plan(seconds=1e-12)


def test_a_raw_member_needs_a_span():
    analyzer, _, _ = two_rate_group()
    with pytest.raises(ValueError, match="needs a capture span"):
        analyzer.plan()


def test_a_member_without_a_capture_clock_needs_an_override():
    trigger = FakeTrigger()
    timed = SyntheticControl("timed.control", trigger=trigger,
                             sample_rate=100_000_000)
    unclocked = SyntheticControl("unclocked.control", trigger=trigger,
                                 sample_rate=0)
    analyzer = group(timed, unclocked)
    with pytest.raises(ValueError, match="no capture clock"):
        analyzer.plan(seconds=10e-6)
    # The caller supplying raw counts is the only way to capture it, and those
    # counts stand exactly as given.
    plan = analyzer.plan(seconds=10e-6,
                          overrides={"unclocked.control": {"count": 128,
                                                           "pretrigger": 16}})
    assert plan.params("unclocked.control") == {"count": 128, "pretrigger": 16,
                                                "windows": 1}
    assert plan.params("timed.control")["count"] == 1000


def test_an_override_wins_over_the_derivation_and_is_reported():
    analyzer, _, _ = two_rate_group()
    plan = analyzer.plan(seconds=10e-6,
                          overrides={"lb.control": {"count": 64}})
    assert plan.params("lb.control") == {"count": 64, "pretrigger": 0,
                                         "windows": 1}
    assert plan.params("gen.control")["count"] == 1000
    # The member no longer covers the group's window; that is not silent.
    assert any("capturing 64" in note for note in plan.notes)
    with pytest.raises(ValueError, match="no group member"):
        analyzer.plan(seconds=10e-6, overrides={"nope": {}})


def test_a_read_reuses_the_window_the_arm_resolved():
    analyzer, slow, fast = two_rate_group()
    asyncio.run(analyzer.configure_and_arm(seconds=10e-6, pre_seconds=2e-6))
    assert slow.params == {"count": 1000, "pretrigger": 200, "windows": 1}
    assert fast.params == {"count": 2000, "pretrigger": 400, "windows": 1}
    assert analyzer.armed_plan.params("gen.control") == slow.params
    # A read that refines one field keeps the rest of the armed window.
    plan = analyzer.plan_for_read(None, 1e-6, None, None, None)
    assert plan.params("gen.control") == {"count": 1000, "pretrigger": 100,
                                          "windows": 1}
    # ... and one that repeats nothing is exactly the armed plan.
    assert analyzer.plan_for_read(None, None, None, None, None) \
        is analyzer.armed_plan


def test_a_read_before_any_arm_needs_a_window():
    analyzer, _, _ = two_rate_group()
    with pytest.raises(ValueError, match="has not been armed"):
        analyzer.plan_for_read(None, None, None, None, None)


def test_the_window_fields_are_checked():
    analyzer, _, _ = two_rate_group()
    with pytest.raises(ValueError, match="pre-trigger span"):
        analyzer.plan(seconds=1e-6, pre_seconds=2e-6)
    with pytest.raises(ValueError, match="must be positive"):
        analyzer.plan(seconds=0)
    with pytest.raises(TypeError, match="no field"):
        GroupWindow().merged(count=32)


@pytest.mark.parametrize("text,seconds", [
    ("10us", 10e-6), ("10µs", 10e-6), ("1.5ms", 1.5e-3), ("800ns", 800e-9),
    ("2s", 2.0), ("0.5", 0.5), (" 3 ms ", 3e-3), (None, None),
])
def test_durations_parse_with_or_without_a_unit(text, seconds):
    assert Duration.parse(text) == seconds


def test_a_duration_that_is_not_one_is_refused():
    with pytest.raises(ValueError, match="not a duration"):
        Duration.parse("soon")


# -- run-length-encoded and mixed groups ------------------------------------

def mixed_group():
    """A group whose members store differently: the parameter vocabularies
    that a group has to dispatch on."""
    trigger = FakeTrigger()
    raw = SyntheticControl("gen.control", trigger=trigger,
                           sample_rate=100_000_000, max_length=4096,
                           depth=8192)
    rle = SyntheticRleControl("lb.control", trigger=trigger,
                              sample_rate=200_000_000, depth=256)
    return group(raw, rle), raw, rle


def test_each_member_gets_the_parameters_of_its_own_kind():
    analyzer, _, _ = mixed_group()
    assert analyzer.member_kinds == {"gen.control": "raw", "lb.control": "rle"}
    plan = analyzer.plan(seconds=10e-6, pre_seconds=2e-6, pre_lines=32)
    # The raw member counts samples; the RLE one caps the post-trigger time and
    # keeps a ring of lines, whose span the captured data decides.
    assert plan.params("gen.control") == {"count": 1000, "pretrigger": 200,
                                          "windows": 1}
    assert plan.params("lb.control") == {"pre_lines": 32,
                                         "max_seconds": pytest.approx(8e-6)}
    rle_plan = next(m for m in plan if m.name == "lb.control")
    assert rle_plan.kind == "rle" and rle_plan.pre_seconds is None
    assert rle_plan.pre_lines == 32
    assert "ring 32 line(s)" in rle_plan.summary()
    assert plan.notes == []


def test_an_rle_group_captures_until_the_buffer_fills_without_a_span():
    trigger = FakeTrigger()
    a = SyntheticRleControl("a.control", trigger=trigger,
                            sample_rate=100_000_000, depth=256)
    b = SyntheticRleControl("b.control", trigger=trigger,
                            sample_rate=200_000_000, depth=256)
    analyzer = group(a, b)
    plan = analyzer.plan(pre_lines=16)
    for name in ("a.control", "b.control"):
        assert plan.params(name) == {"pre_lines": 16, "max_seconds": 0.0}
    assert "until the buffer fills" in plan.lines()[0]
    # A ring deeper than the buffer is clamped, and said so.
    plan = analyzer.plan(pre_lines=4096)
    assert plan.params("a.control")["pre_lines"] == 255
    assert len(plan.notes) == 2 and "exceeds the 256-line buffer" in plan.notes[0]


def test_an_rle_member_without_a_capture_clock_cannot_be_time_capped():
    trigger = FakeTrigger()
    rle = SyntheticRleControl("rle.control", trigger=trigger, sample_rate=0)
    with pytest.raises(ValueError, match="post-trigger cap"):
        group(rle).plan(seconds=10e-6)
    # Without a cap it runs until the buffer fills, which needs no clock.
    assert group(rle).plan(pre_lines=8).params("rle.control") == {
        "pre_lines": 8, "max_seconds": 0.0}


def test_a_group_with_an_rle_member_captures_a_single_window():
    analyzer, _, _ = mixed_group()
    with pytest.raises(ValueError, match="single window"):
        analyzer.plan(seconds=10e-6, windows=2)


def test_arming_a_mixed_group_arms_each_member_in_its_own_vocabulary():
    analyzer, raw, rle = mixed_group()
    plan = asyncio.run(analyzer.configure_and_arm(seconds=10e-6,
                                                   pre_seconds=2e-6,
                                                   pre_lines=24))
    assert raw.params == {"count": 1000, "pretrigger": 200, "windows": 1}
    assert rle.params == {"pre_lines": 24, "max_seconds": pytest.approx(8e-6)}
    assert plan.summary().startswith("gen.control: 1000 samples (10.0 µs), "
                                     "200 pre-trigger (2.0 µs), 1 window(s)")


def test_the_pane_is_told_what_its_members_take():
    raw_only, _, _ = two_rate_group()
    mixed, _, _ = mixed_group()
    trigger = FakeTrigger()
    rle_only = group(SyntheticRleControl("a.control", trigger=trigger,
                                         sample_rate=100_000_000))
    for analyzer, composition in ((raw_only, "raw"), (mixed, "mixed"),
                                   (rle_only, "rle")):
        meta = AnalyzerGui(analyzer, None).describe()
        assert meta["composition"] == composition
        assert [m["kind"] for m in meta["members"]] == list(
            analyzer.member_kinds.values())


def test_the_pane_may_only_send_window_fields():
    with pytest.raises(ValueError, match="has no field"):
        AnalyzerGui.window({"count": 32})
    assert AnalyzerGui.window({"seconds": 1e-6, "pre_lines": None}) == {
        "seconds": 1e-6}


@pytest.mark.parametrize("states,expected", [
    ([(Control.STATE_IDLE, True, 1), (Control.STATE_IDLE, True, 1)],
     (Control.STATE_IDLE, True, 1)),
    ([(Control.STATE_ARMED, False, 0), (Control.STATE_IDLE, False, 0)],
     (Control.STATE_ARMED, False, 0)),
    ([(Control.STATE_ARMED, False, 0), (Control.STATE_CAPTURING, True, 0)],
     (Control.STATE_CAPTURING, False, 0)),
    ([(Control.STATE_IDLE, True, 2), (Control.STATE_IDLE, True, 1)],
     (Control.STATE_IDLE, True, 1)),
])
def test_group_state_composes_its_members(states, expected):
    trigger = FakeTrigger()
    controls = [SyntheticControl(f"m{i}.control", trigger=trigger,
                                 sample_rate=100_000_000)
                for i in range(len(states))]
    for control, state in zip(controls, states):
        control.state = state
    assert asyncio.run(group(*controls).status()) == expected


@pytest.mark.parametrize("state,triggered,expected", [
    (Control.STATE_IDLE, False, ("idle", "idle")),
    (Control.STATE_IDLE, True, ("idle", "idle")),       # capture done, back home
    (Control.STATE_ARMED, False, ("armed", "active")),
    (Control.STATE_CAPTURING, False, ("capturing", "active")),
    (Control.STATE_CAPTURING, True, ("capturing", "attention")),
    (Control.STATE_READING, True, ("reading", "attention")),
])
def test_a_poll_names_and_tones_its_own_state(state, triggered, expected):
    # The state encoding never leaves the analyzer: a poll carries the name the
    # console prints and a tone out of the shell's fixed vocabulary.
    poll = Control.reported({"state": state, "triggered": triggered,
                             "fingerprint": 1, "progress": "", "fetch": None})
    assert (poll["state"], poll["tone"]) == expected
    assert poll["fingerprint"] == 1 and poll["fetch"] is None


def test_a_group_capture_only_renders_as_vcd():
    trigger = FakeTrigger()
    controls = [SyntheticControl("a.control", trigger=trigger,
                                 sample_rate=100_000_000),
                SyntheticControl("b.control", trigger=trigger,
                                 sample_rate=125_000_000)]
    analyzer = group(*controls)
    result = {"kind": "group", "members": [
        {"name": "a.control",
         "result": raw_result(["s"], 100_000_000, [[0, 1]], 1)},
        {"name": "b.control",
         "result": raw_result(["w"], 125_000_000, [[0, 1]], 1)}]}
    console = AnalyzerConsole(analyzer)
    assert console.render(result, "vcd").startswith(b"$comment")
    with pytest.raises(ValueError, match="one timebase per member"):
        console.render(result, "csv")
