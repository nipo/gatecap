"""Resolve a gatecap node from a resource path and run one capture.

Usage:
  acrobe run capture_demo.py                       # starts the socket sim
  acrobe run capture_demo.py udp/HOST:PORT/gatecap # already-running target

The interesting part is `root(path)`: acrobe walks the path, spawns the
gatecap Bridge over the datagram, discovers and starts its subtree, and
hands back the Bridge node. From there it is plain acrobe node API.
`acrobe run` supplies the event loop, plugin loading and teardown around
`main()`.
"""

import asyncio
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

from acrobe.root import root
from acrobe_plugin.gatecap.instrument.la.blocks.control import Control

SIM = os.path.join(HERE, os.pardir, "gateware", "example", "socket", "tb")
DEFAULT_PATH = "udp/127.0.0.1:4242/gatecap"


async def run(path):
    r = await root(path)

    control, = r.children_of_class(Control)
    buffer = control.sink_node_get()
    trigger = control.trigger_node_get()

    # Match-all trigger (mask 0), post-trigger capture of 8 samples.
    await trigger.configure(value=0, mask=0)
    await control.configure(length=8)
    await control.arm()
    triggered, _done = await control.wait_done()
    head = await control.head(0)
    samples = await buffer.read_window(head, 8, control.signal_count)
    print("triggered:", triggered, "samples:", samples)

    assert triggered, "expected a trigger"
    assert len(samples) == 8, samples
    for i in range(1, len(samples)):
        assert samples[i] == (samples[0] + i) & 0xFF, samples
    print("PASSED: captured 8 consecutive samples")


async def main():
    if len(sys.argv) > 1:
        # Target already running; just resolve and drive it.
        await run(sys.argv[1])
        return

    if not os.path.exists(SIM):
        raise SystemExit(f"simulator not built: {SIM}")
    sim = subprocess.Popen([SIM, "--ieee-asserts=disable"])

    try:
        await asyncio.sleep(1.0)  # let the sim bind the UDP port
        await run(DEFAULT_PATH)
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sim.kill()
