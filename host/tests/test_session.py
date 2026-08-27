"""End-to-end test of the headless Session against the socket simulator.

Builds and runs the socket sim, then drives acrobe_plugin.gatecap.session.Session
through enumerate -> describe -> capture -> VCD, in a single event loop.

Run: python3.13 -m pytest host/tests/test_session.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.instrument.la.blocks.control import Control
from acrobe_plugin.gatecap.instrument.la.waveform import WaveformView
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket")
RESOURCE = "udp/127.0.0.1:4242/gatecap"


@pytest.fixture(scope="module")
def sim():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket"], capture_output=True)
    time.sleep(0.5)
    build = subprocess.run(["gbs", "project", "build"], cwd=SIM_DIR,
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    sim_bin = os.path.join(SIM_DIR, "tb")
    assert os.path.exists(sim_bin), "simulator executable missing after build"
    proc = subprocess.Popen([sim_bin, "--ieee-asserts=disable"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    reset_hw_root_for_tests()  # fresh hw tree, not one cached by another module
    try:
        yield
    finally:
        reset_hw_root_for_tests()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["pkill", "-9", "-f", "gateware/example/socket"],
                       capture_output=True)


async def _run():
    s = Session(RESOURCE)
    # open() enumerates the tree and returns the instance fingerprint.
    fp = await s.open()
    assert isinstance(fp, int) and fp != 0 and s.fingerprint == fp
    assert await s.instance_changed() is False   # same instance -> no change
    assert await s.health() is True

    # The fingerprint protocol is generic: every block that exposes one is read
    # at open, so a poll answered from host memory can still report it.
    seeded = s.fingerprinted()
    assert seeded and all(b.last_fingerprint == fp for b in seeded)

    (control,) = s.blocks_of(Control)   # one raw control block
    # Shared capture-parameter validation (the GUI and CLI both use it).
    with pytest.raises(ValueError):
        control.validate_capture(count=1024)              # exceeds depth 64
    with pytest.raises(ValueError):
        control.validate_capture(count=16, pretrigger=16)  # pretrigger >= count
    with pytest.raises(ValueError):
        control.validate_capture(count=16, windows=8)      # 16*8 > 64
    control.validate_capture(count=16, pretrigger=2)       # valid: no raise
    assert control.signal_count == 8
    assert control.signal_names == [f"count{i}" for i in range(8)]
    assert control.sink_node_get().depth == 64
    assert control.trigger_node_get().signal_count == 8  # trigger's own vector

    # Match-all trigger: the free-running counter, consecutive.
    r = await control.capture(0, 0, count=16)
    samples = [x for win in r["windows"] for x in win]
    assert len(samples) == 16
    for i in range(1, 16):
        assert samples[i] == (samples[0] + i) & 0xFF, samples

    # Value trigger: the trigger sample is exactly the matched value (proves
    # the trigger split + back-dating end to end through the driver).
    r2 = await control.capture(0x80, 0xFF, count=4)
    assert [x for w in r2["windows"] for x in w] == [0x80, 0x81, 0x82, 0x83]
    assert r2["triggered"] is True

    # VCD export for Surfer: valid, signals under the capture scope, one
    # trigger marker for a single-window capture.
    vcd, markers = WaveformView().to_vcd(r2)
    text = vcd.decode()
    assert text.startswith("$") and "$enddefinitions $end" in text
    assert "$scope module capture $end" in text and "count0 $end" in text
    assert markers == [("trigger", 0)]  # pretrigger 0 -> trigger at t=0

    # Reconnect: tear the transport out and rebuild it in place (models a
    # device re-enumeration / reprogram). A fresh transport is summoned, the
    # reference fingerprint is kept (so a reprogram would still be flagged),
    # and captures resume.
    old_node = s.node
    fp2 = await s.reconnect()
    assert fp2 == fp and s.node is not old_node   # a genuinely fresh subtree
    assert await s.instance_changed() is False     # same gateware -> not stale
    (control2,) = s.blocks_of(Control)
    r3 = await control2.capture(0x80, 0xFF, count=4)
    assert [x for w in r3["windows"] for x in w] == [0x80, 0x81, 0x82, 0x83]

    # The status poll folds state, trigger, fingerprint and progress into one
    # burst read of the contiguous live block (raw: STATUS..FINGERPRINT).
    st, tr, _ = await control2.status()
    p = await control2.poll()
    assert (p["state"], p["triggered"], p["fingerprint"]) == (
        Control.STATE_NAMES[st], tr, await control2.fingerprint())
    assert p["tone"] in ("idle", "active", "attention")


class FingerprintBlock:
    """A block that answers the fingerprint protocol and nothing else -- what
    a third-party instrument needs to expose to take part in change
    detection."""

    def __init__(self, value):
        self.value = value
        self.reads = 0

    async def fingerprint(self):
        self.reads += 1
        return self.value


class FakeNode:
    def __init__(self, children):
        self.children = children

    def children_find(self, predicate):
        return [child for child in self.children if predicate(child)]


def test_the_fingerprint_protocol_names_no_driver_class():
    # Change detection and health ride on fingerprint(), not on a class the
    # Session knows: a block of any type takes part, one without it is passed
    # over.
    session = Session("none")
    block = FingerprintBlock(0x1234)
    session.node = FakeNode([object(), block])
    assert session.fingerprinted() == [block]

    session.fingerprint = 0x1234
    assert asyncio.run(session.instance_changed()) is False
    assert asyncio.run(session.health()) is True
    block.value = 0x5678
    assert asyncio.run(session.instance_changed()) is True
    assert block.reads == 3

    session.node = FakeNode([object()])   # nothing answers -> nothing to check
    assert asyncio.run(session.instance_changed()) is False
    assert asyncio.run(session.health()) is False


def test_multiwindow_markers():
    # to_vcd's marker layout for a multi-window raw capture -- a pure check on
    # a synthetic result (the socket bench is single-window).
    result = {"kind": "raw", "names": ["a", "b"], "signal_count": 2,
              "sample_rate": 100_000_000,   # -> 10000 ps/sample
              "windows": [list(range(8)), list(range(8)), list(range(8))],
              "trigger_index": 2}
    _, markers = WaveformView().to_vcd(result)
    assert [n for n, _ in markers] == ["trig0", "w1", "trig1", "w2", "trig2"]
    m = dict(markers)
    assert m["trig0"] == 2 * 10000        # window 0 trigger
    assert m["w1"] == 8 * 10000           # window 1 boundary
    assert m["trig1"] == (8 + 2) * 10000  # window 1 trigger


def test_rle_trigger_latency_marker():
    # The RLE trigger marker is skewed back into the pre-region by the
    # trigger's latency (the pre-region holds those cycles).
    result = {"kind": "rle", "names": ["a"], "signal_count": 1,
              "sample_rate": 100_000_000,   # -> 10000 ps/cycle
              "runs": [(0, 5), (1, 3), (1, 4)], "trigger_run": 2,
              "trigger_latency": 2}
    _, markers = WaveformView().to_vcd(result)
    # pre-region = 5 + 3 = 8 cycles; skewed back 2 -> trigger at cycle 6.
    assert markers == [("trigger", 6 * 10000)]


def test_session_socket(sim):
    asyncio.run(_run())
