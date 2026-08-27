"""Host edge-trigger path against the socket_edge simulator.

Enumerate the edge trigger block, program a rising-edge term through the
driver, and confirm the raw capture lands the trigger on the edge cycle; plus
the CLI edge capture.

Run: python3.13 -m pytest host/tests/test_edge.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.instrument.la.blocks.control import Control
from acrobe_plugin.gatecap.instrument.la.blocks.trigger import EdgeTrigger
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_edge")
RESOURCE = "udp/127.0.0.1:4246/gatecap"


@pytest.fixture(scope="module")
def sim():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_edge"],
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
    reset_hw_root_for_tests()
    try:
        yield
    finally:
        reset_hw_root_for_tests()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_edge"],
                       capture_output=True)


async def _run():
    s = Session(RESOURCE)
    await s.open()

    (control,) = s.blocks_of(Control)
    trig = control.trigger_node_get()
    assert isinstance(trig, EdgeTrigger)
    assert trig.signal_names == [f"count{i}" for i in range(8)]

    # count0 rising: new bit0 = 1, old bit0 = 0, both masked.
    await trig.configure(0x01, 0x01, 0x00, 0x01)
    await control.configure_and_arm(count=8, pretrigger=2)

    # count0 toggles every cycle, so it fires immediately and fills fast.
    state = None
    for _ in range(100):
        s0, triggered, _ = await control.status()
        state = s0
        if s0 == control.STATE_IDLE:
            break
        await asyncio.sleep(0.005)
    assert state == control.STATE_IDLE and triggered

    r = await control.read_trace(count=8, pretrigger=2)
    samples = [x for win in r["windows"] for x in win]
    # The trigger sample sits at pretrigger=2: count0 must be 1 there (the
    # 0->1 destination cycle) and 0 the cycle before -- proves the edge match
    # and the back-dating land it on the new-value cycle.
    assert samples[2] & 1 == 1
    assert samples[1] & 1 == 0

    await s.close()


def test_edge_socket(sim):
    asyncio.run(_run())


def test_edge_cli(sim, tmp_path):
    reset_hw_root_for_tests()
    out = tmp_path / "edge.vcd"
    cap = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "capture", "control.control",
         "--trigger", "count0=rising", "--count", "8", "--output", str(out)],
        capture_output=True, text=True, timeout=30)
    assert cap.returncode == 0, cap.stderr
    assert "new=0x1/0x1 old=0x0/0x1" in cap.stderr
    text = out.read_text()
    assert text.startswith("$") and "$enddefinitions $end" in text

    info = subprocess.run(["acrobe", "gatecap", "-r", RESOURCE, "info"],
                          capture_output=True, text=True, timeout=30)
    assert info.returncode == 0, info.stderr
    assert "edge/transition match" in info.stdout
