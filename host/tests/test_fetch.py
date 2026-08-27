"""Trace-readback progress: what a status poll reports while the trace is
coming over the transport.

A slow link (JTAG: seconds for a few thousand words) makes the readback long
enough to be visible, so a poll must report the transfer instead of stale
hardware state -- and must not add traffic to the link the transfer is
saturating. These tests drive the drivers against an in-memory bridge that
calls back after every transaction, which is where the poll under test runs:
mid-fetch, deterministically, with the bridge's own call count as the witness
that it stayed off the wire.

Run: python3.13 -m pytest host/tests/test_fetch.py
"""

import asyncio
import uuid

import pytest

from acrobe_plugin.gatecap.instrument.la.driver import (
    LOGIC_ANALYZER_UUID, LogicAnalyzer)
from acrobe_plugin.gatecap.rack import Instrument as Envelope
from acrobe_plugin.gatecap.instrument.la.blocks.buffer import BUFFER_UUID, Buffer
from acrobe_plugin.gatecap.instrument.la.blocks.control import (CONTROL_UUID,
                                                  RLE_CONTROL_UUID, Control,
                                                  RleControl)
from acrobe_plugin.gatecap.instrument.la.fetch import FetchProgress

FINGERPRINT = 0xC0FFEE01


class FakeBridge:
    """Word-addressed memory behind the bridge API. Every read is one
    transaction, counted; ``on_read`` runs after each of them, which is where a
    test observes a poll arriving mid-fetch (the hook is detached while it
    runs, so a poll that does read is counted, not recursed into)."""

    word_bytes = 4
    max_burst = 16

    def __init__(self):
        self.words = {}
        self.calls = 0
        self.on_read = None

    def word_set(self, addr, value):
        self.words[addr] = value

    async def mem_read(self, addr, size):
        self.calls += 1
        data = b"".join(
            self.words.get(addr + i * self.word_bytes, 0)
            .to_bytes(self.word_bytes, "little")
            for i in range(size // self.word_bytes))
        hook, self.on_read = self.on_read, None
        if hook is not None:
            await hook()
            self.on_read = hook
        return data

    async def mem_write(self, addr, data):
        self.calls += 1
        for i in range(0, len(data), self.word_bytes):
            self.words[addr + i] = int.from_bytes(
                data[i:i + self.word_bytes], "little")

    async def read32(self, addr):
        return int.from_bytes(await self.mem_read(addr, self.word_bytes),
                              "little")

    async def write32(self, addr, value):
        await self.mem_write(addr, value.to_bytes(self.word_bytes, "little"))


class FakeControl(Control):
    """A raw control on the fake bridge, with its sink buffer handed over
    instead of resolved through an enumerated sibling map."""

    def __init__(self, bridge, buffer, name="control", base=0x1000,
                 max_windows=4):
        super().__init__(bridge, base, name,
                         [CONTROL_UUID, "buffer", "trigger", 8, "b[7:0]", 1024,
                          max_windows, 100_000_000, 0])
        self.buffer = buffer

    def sink_node_get(self):
        return self.buffer


class FakeTrigger:
    """Stands in for the trigger block: an RLE result carries its latency."""

    LATENCY = 1
    name = "trigger"


class FakeRleControl(RleControl):
    def __init__(self, bridge, buffer, name="rle.control", base=0x2000):
        super().__init__(bridge, base, name,
                         [RLE_CONTROL_UUID, "buffer", "trigger", 8, "b[7:0]",
                          100_000_000, 0])
        self.buffer = buffer

    def sink_node_get(self):
        return self.buffer

    def trigger_node_get(self):
        return FakeTrigger()


def make_buffer(bridge, base=0x8000, stride=32, size_l2=12):
    return Buffer(bridge, base, "buffer", [BUFFER_UUID, stride, size_l2])


def status_seed(bridge, base, state=Control.STATE_IDLE, triggered=True):
    """The status group a poll reads: STATUS then FINGERPRINT."""
    bridge.word_set(base + Control.REG_STATUS, state | (0x4 if triggered else 0))
    bridge.word_set(base + Control.REG_FINGERPRINT, FINGERPRINT)


class Observer:
    """Polls a block after every bridge transaction of a fetch and records what
    it saw, together with the traffic the poll itself caused."""

    def __init__(self, bridge, block):
        self.bridge = bridge
        self.block = block
        self.polls = []      # (payload, bridge calls the poll made)

    async def __call__(self):
        before = self.bridge.calls
        payload = await self.block.poll()
        self.polls.append((payload, self.bridge.calls - before))

    @property
    def reading(self):
        return [p for p, _ in self.polls if p["state"] == "reading"]

    def assert_silent_while_reading(self):
        for payload, calls in self.polls:
            if payload["state"] == "reading":
                assert calls == 0, "a poll went to the hardware during a fetch"

    def assert_progresses(self, total):
        """The reported fraction only ever grows, actually moves, and keeps one
        denominator once the readback has sized itself. A poll landing before
        that (during the register reads that size the fetch) reports no size
        and nothing done; one landing between a burst arriving and its words
        being counted repeats the previous fraction -- it must never come back
        down."""
        sized = [p["fetch"] for p in self.reading if p["fetch"]["total"]]
        unsized = [p["fetch"] for p in self.reading if not p["fetch"]["total"]]
        assert sized, "no poll landed inside the transfer"
        assert all(f["done"] == 0 and f["fraction"] == 0.0 for f in unsized)
        assert all(f["total"] == total for f in sized), "the denominator moved"
        fractions = [f["fraction"] for f in sized]
        assert fractions == sorted(fractions), fractions
        assert len(set(fractions)) >= 3, fractions
        assert all(0.0 <= f <= 1.0 for f in fractions)


# -- transfer planning ------------------------------------------------------

def test_planned_words_match_the_geometry():
    bridge = FakeBridge()
    # One sample per 32-bit word.
    plain = make_buffer(bridge)
    assert plain.transfer_words(0, 64) == 64
    assert plain.window_words(37, 64) == 64
    # Four 8-bit samples per word: a window whose slot starts on a word costs a
    # quarter of the words, and a slot that does not adds the straddled word.
    packed = make_buffer(bridge, stride=8)
    assert packed.transfer_words(0, 64) == 16
    assert packed.transfer_words(2, 64) == 17
    assert packed.window_words(70, 64) == 16      # slot 64, word-aligned
    assert packed.window_words(11, 10) == 3       # slot 10, straddles a word
    # A wide sample spans a run of words.
    wide = make_buffer(bridge, stride=64)
    assert wide.transfer_words(0, 64) == 128
    assert plain.transfer_words(0, 0) == 0


def test_nested_fetches_keep_the_outer_counters():
    # A group read marks a member fetching for the whole group transfer; the
    # member's own readback nests inside without zeroing what it counted.
    progress = FetchProgress()
    progress.begin()
    progress.begin()
    progress.expect(40)
    progress.advance(10)
    progress.end()
    assert progress.active and progress.done == 10 and progress.total == 40
    progress.end()
    assert not progress.active


# -- raw readback -----------------------------------------------------------

def test_raw_fetch_reports_progress_without_touching_the_bridge():
    bridge = FakeBridge()
    buffer = make_buffer(bridge)
    control = FakeControl(bridge, buffer)
    status_seed(bridge, control.base)
    bridge.word_set(control.base + control.REG_HEAD_BASE, 0)

    async def run():
        # The GUI polls before it reads; that is what seeds the status a
        # mid-fetch poll stands in for.
        first = await control.poll()
        assert first["state"] == "idle" and first["fetch"] is None
        observer = Observer(bridge, control)
        bridge.on_read = observer
        result = await control.read_trace(count=256, windows=1)
        bridge.on_read = None
        assert len(result["windows"][0]) == 256
        return observer, await control.poll()

    observer, after = asyncio.run(run())
    observer.assert_silent_while_reading()
    observer.assert_progresses(total=256)          # one word per sample
    # The counter ends on the plan: every word of the transfer was accounted.
    assert control.fetch.done == control.fetch.total == 256
    # The hardware status a poll cannot read is the last one that was read.
    for payload in observer.reading:
        assert payload["fingerprint"] == FINGERPRINT
        assert payload["triggered"] is True
        assert "words" in payload["progress"]
    # The fetch is over: the poll goes back to the hardware and reports it.
    assert control.fetch.active is False
    assert after["state"] == "idle" and after["fetch"] is None


def test_multi_window_fetch_has_one_denominator():
    bridge = FakeBridge()
    buffer = make_buffer(bridge)
    control = FakeControl(bridge, buffer)
    status_seed(bridge, control.base)
    # Three windows in their own slots, each rolled to a different head.
    for window, head in enumerate([5, 64 + 17, 128 + 63]):
        bridge.word_set(control.base + control.REG_HEAD_BASE + window * 4, head)

    async def run():
        await control.poll()
        observer = Observer(bridge, control)
        bridge.on_read = observer
        await control.read_trace(count=64, windows=3)
        bridge.on_read = None
        return observer

    observer = asyncio.run(run())
    observer.assert_silent_while_reading()
    # All three windows are planned before the first one is read, so the bar
    # does not restart or jump at a window boundary.
    observer.assert_progresses(total=3 * 64)


def test_wide_fetch_counts_the_words_a_sample_spans():
    bridge = FakeBridge()
    buffer = make_buffer(bridge, stride=64)      # two words per sample
    control = FakeControl(bridge, buffer)
    status_seed(bridge, control.base)
    bridge.word_set(control.base + control.REG_HEAD_BASE, 0)

    async def run():
        await control.poll()
        observer = Observer(bridge, control)
        bridge.on_read = observer
        await control.read_trace(count=128, windows=1)
        bridge.on_read = None
        return observer

    observer = asyncio.run(run())
    observer.assert_silent_while_reading()
    observer.assert_progresses(total=256)


# -- RLE readback -----------------------------------------------------------

def test_rle_fetch_covers_both_regions():
    bridge = FakeBridge()
    buffer = make_buffer(bridge)
    control = FakeRleControl(bridge, buffer)
    status_seed(bridge, control.base)
    end = 200
    bridge.word_set(control.base + control.REG_END_PTR, end)
    bridge.word_set(control.base + control.REG_PRE_HEAD, 3)
    bridge.word_set(control.base + control.REG_PRE_N, 8)

    async def run():
        await control.configure(pre_lines=8)
        await control.poll()
        observer = Observer(bridge, control)
        bridge.on_read = observer
        await control.read_trace()
        bridge.on_read = None
        return observer

    observer = asyncio.run(run())
    observer.assert_silent_while_reading()
    # The pre-trigger ring read from zero plus the post region up to the end
    # pointer, planned together from the one end-pointer read.
    observer.assert_progresses(total=end)


# -- correlated group -------------------------------------------------------

def analyzer_over(controls):
    """A logic analyzer over already-built controls: the envelope names them
    the way the gateware's does, and the children are handed over instead of
    being enumerated."""
    analyzer = LogicAnalyzer(None, 0x4000, Envelope(
        name="la", type_uuid=LOGIC_ANALYZER_UUID, base=0x4000, size_l2=14,
        children={}, tail=[[control.name for control in controls]]))
    analyzer.siblings_resolve({c.name: c for c in controls})
    return analyzer


def test_group_fetch_composes_its_members():
    bridge = FakeBridge()
    controls = []
    for index in range(2):
        base = 0x1000 + index * 0x1000
        buffer = make_buffer(bridge, base=0x8000 + index * 0x4000)
        control = FakeControl(bridge, buffer, name=f"m{index}.control", base=base)
        status_seed(bridge, base)
        bridge.word_set(base + Control.REG_HEAD_BASE, 0)
        controls.append(control)
    analyzer = analyzer_over(controls)

    async def run():
        for control in controls:
            await control.poll()
        assert (await analyzer.poll())["state"] == "idle"
        group = Observer(bridge, analyzer)
        member = Observer(bridge, controls[1])

        async def hook():
            await group()
            await member()

        bridge.on_read = hook
        # These members carry no capture clock, so the group cannot derive a
        # window for them; per-member counts are how such a group is read.
        result = await analyzer.read_trace(
            overrides={control.name: {"count": 128} for control in controls})
        bridge.on_read = None
        assert [m["name"] for m in result["members"]] == [c.name for c in controls]
        return group, member, await analyzer.poll()

    group, member, after = asyncio.run(run())
    group.assert_silent_while_reading()
    member.assert_silent_while_reading()
    # One denominator over the whole group: both members are sized before the
    # first word of either moves.
    group.assert_progresses(total=2 * 128)
    # A member is "reading" for the whole group transfer, not only during its
    # own turn -- its pane must not poll the link the group read owns.
    assert len(member.reading) == len(member.polls)
    assert after["state"] == "idle" and after["fetch"] is None


def test_group_reports_a_member_reading_on_its_own():
    # A member read from its own pane still shows up on the group pane, sized
    # by that member alone (the group planned nothing).
    bridge = FakeBridge()
    buffer = make_buffer(bridge)
    control = FakeControl(bridge, buffer, name="m0.control")
    status_seed(bridge, control.base)
    bridge.word_set(control.base + control.REG_HEAD_BASE, 0)
    analyzer = analyzer_over([control])

    async def run():
        await control.poll()
        observer = Observer(bridge, analyzer)
        bridge.on_read = observer
        await control.read_trace(count=64, windows=1)
        bridge.on_read = None
        return observer

    observer = asyncio.run(run())
    observer.assert_silent_while_reading()
    observer.assert_progresses(total=64)


def test_a_fetch_that_raises_still_ends():
    # The progress must not stay stuck "reading" (and poll-silent) when the
    # transport drops mid-transfer.
    class Failing(FakeBridge):
        async def mem_read(self, addr, size):
            if addr >= 0x8000:
                raise ConnectionError("link dropped")
            return await super().mem_read(addr, size)

    bridge = Failing()
    control = FakeControl(bridge, make_buffer(bridge))
    status_seed(bridge, control.base)
    bridge.word_set(control.base + control.REG_HEAD_BASE, 0)

    async def run():
        with pytest.raises(ConnectionError):
            await control.read_trace(count=64, windows=1)
        return await control.poll()

    assert control.fetch.active is False
    assert asyncio.run(run())["state"] == "idle"
