"""Host wide-capture path against the socket_wide simulator.

Exercises a capture wider than one APB word (48 signals -> two 32-bit words per
sample): the buffer stores 48-bit lines and the host reads two words per sample.
The bench's free-running 48-bit counter makes the trace consecutive.

Run: python3.13 -m pytest host/tests/test_wide.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.instrument.la.blocks.control import Control
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_wide")
RESOURCE = "udp/127.0.0.1:4245/gatecap"


@pytest.fixture(scope="module")
def sim():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_wide"],
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
        subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_wide"],
                       capture_output=True)


async def _run():
    s = Session(RESOURCE)
    await s.open()
    (control,) = s.blocks_of(Control)
    assert control.signal_count == 48

    # A 48-bit sample spans two 32-bit words: the buffer advertises a 64-bit
    # stride and reads two words per sample.
    buf = control.sink_node_get()
    assert buf.sample_stride == 64
    assert buf.words_per_sample == 2

    # Match-all trigger on the (8-bit) trigger vector: capture the free-running
    # 48-bit counter, consecutive and full-width.
    r = await control.capture(0, 0, count=16)
    vals = [x for w in r["windows"] for x in w]
    assert len(vals) == 16
    # The counter exceeds 32 bits' worth of steps only after billions of cycles,
    # so within a window the high words are 0 -- but the value must still be a
    # full 48-bit field and strictly consecutive.
    for i in range(1, 16):
        assert vals[i] == (vals[0] + i) & ((1 << 48) - 1), [hex(v) for v in vals]
    assert r["triggered"] is True

    # A value trigger on the low 8 bits lands on that counter phase.
    r2 = await control.capture(0x80, 0xFF, count=4)
    lows = [x & 0xFF for w in r2["windows"] for x in w]
    assert lows == [0x80, 0x81, 0x82, 0x83], [hex(x) for x in lows]


def test_wide_socket(sim):
    asyncio.run(_run())
