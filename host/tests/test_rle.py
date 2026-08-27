"""Host RLE capture path against the socket_rle simulator.

The other host tests drive the raw control; this exercises the RLE control end
to end: enumerate it, run a cycle-capped capture over the bench's slow counter,
check the decoded runs, the shared progress string, and the change-based VCD,
plus the CLI capture path.

Run: python3.13 -m pytest host/tests/test_rle.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.instrument.la.blocks.control import RleControl
from acrobe_plugin.gatecap.instrument.la.waveform import WaveformView
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_rle")
RESOURCE = "udp/127.0.0.1:4244/gatecap"


@pytest.fixture(scope="module")
def sim():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_rle"],
                   capture_output=True)
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
        subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_rle"],
                       capture_output=True)


async def _run():
    s = Session(RESOURCE)
    await s.open()

    # One RLE control with its own trigger and buffer.
    (control,) = s.blocks_of(RleControl)
    assert isinstance(control, RleControl)
    assert control.signal_count == 8
    assert control.signal_names == [f"b{i}" for i in range(8)]
    assert control.sink_node_get().depth == 1024
    assert control.trigger_node_get().signal_count == 8

    # The bench signal is a counter that only advances every 100 cycles, so a
    # naive capture would fill slowly; the cycle cap bounds it. Match-all
    # trigger (fires immediately), post-trigger only, ~500-cycle (5 us) cap.
    r = await control.capture(0, 0, pre_lines=0, max_seconds=5e-6)
    assert r["kind"] == "rle" and r["triggered"] is True
    total = sum(dwell for _, dwell in r["runs"])
    assert 490 <= total <= 510, total          # bounded by the cap, not the buffer
    assert len(r["runs"]) >= 4                  # a few counter values in 500 cycles

    # Change-based VCD: valid, signals under the capture scope, trigger at t=0
    # (no pre-trigger region).
    vcd, markers = WaveformView().to_vcd(r)
    text = vcd.decode()
    assert text.startswith("$") and "$enddefinitions $end" in text
    assert "$scope module capture $end" in text and "b0 $end" in text
    assert markers == [("trigger", 0)]

    # The shared progress string (frozen final): elapsed toward the cap + fill.
    prog = await control.progress()
    assert "/ 5.0" in prog and "µs" in prog and "buf" in prog

    await s.close()


def test_rle_socket(sim):
    asyncio.run(_run())


def test_rle_cli(sim, tmp_path):
    reset_hw_root_for_tests()
    out = tmp_path / "rle.vcd"
    # Match-all trigger (no --trigger) fires immediately; the cap completes it.
    cap = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "capture", "control.control",
         "--max-time", "0.000005", "--output", str(out)],
        capture_output=True, text=True, timeout=30)
    assert cap.returncode == 0, cap.stderr
    assert "no trigger" not in cap.stderr           # it did trigger
    text = out.read_text()
    assert text.startswith("$") and "$enddefinitions $end" in text
    assert "b0 $end" in text

    info = subprocess.run(["acrobe", "gatecap", "-r", RESOURCE, "info"],
                          capture_output=True, text=True, timeout=30)
    assert info.returncode == 0, info.stderr
    assert "run-length encoded" in info.stdout
