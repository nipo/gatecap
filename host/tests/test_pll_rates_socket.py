"""End-to-end test of an NSL pll_multi realization over the pll_rates
simulator.

The rack is emitted from ``gateware/example/pll_rates/description.yaml``: one
clock measurer watching the four outputs of a pll_multi instance against the
50 MHz reference the PLL itself runs from, reached over UDP. The PLL is
realized by NSL's simulation backend: outputs run at the rates of the solved
mapping, approximations included.

What is checked here: every measured rate matches the rate the NSL solver
mapped -- three outputs exactly as requested, and out0 (512 x 44.1 kHz,
requested with 500 ppm of slack and fractional division allowed) at the
solver's approximation rather than the request.

Run: python3.13 -m pytest host/tests/test_pll_rates_socket.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.instrument.clock_measurer.driver import ClockMeasurer
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "pll_rates")
RESOURCE = "udp/127.0.0.1:4255/gatecap"

# What description.yaml states.
REFERENCE_HZ = 50_000_000
UPDATE_HZ_L2 = 14
CLOCK_NAMES = ["out0", "out1", "out2", "out3"]

# What the PLL config in tb.vhd requests, and what the NSL solver maps it to
# (deterministic; the bench logs the mapping at elaboration). out0 asks for
# 22_579_200 Hz with 500 ppm of slack; the solver realizes it as VCO 900 MHz
# over the fractional divisor 39+220/256, 22_579_381 Hz.
MAPPED = {
    "out0": 22_579_381,
    "out1": 150_000_000,
    "out2": 100_000_000,
    "out3": 75_000_000,
}

# Error budget of one measurement (see test_clock_rates_socket.py): the
# integer count plus one resynchronised edge, and the window being a whole
# number of reference cycles.
QUANTUM_HZ = 2**UPDATE_HZ_L2
SCALE_PPM = 200


def tolerance(rate_hz):
    return 3 * QUANTUM_HZ + rate_hz * SCALE_PPM / 1e6


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/pll_rates"],
                   capture_output=True)


@pytest.fixture(scope="module")
def sim():
    _kill_stale()
    time.sleep(0.5)
    build = subprocess.run(["gbs", "project", "build"], cwd=SIM_DIR,
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    sim_bin = os.path.join(SIM_DIR, "tb")
    assert os.path.exists(sim_bin), "simulator executable missing after build"
    proc = subprocess.Popen([sim_bin, "--ieee-asserts=disable"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(1.0)  # let the sim bind its UDP port
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
        _kill_stale()


def measurer(session):
    blocks = session.blocks_of(ClockMeasurer)
    assert len(blocks) == 1, [b.name for b in session.blocks()]
    return blocks[0]


def test_every_output_runs_at_the_mapped_rate(sim):
    async def run():
        session = Session(RESOURCE)
        await session.open()
        instrument = measurer(session)
        assert instrument.reference_hz == REFERENCE_HZ
        assert instrument.clock_names == CLOCK_NAMES

        rates = await instrument.rates()
        assert list(rates) == CLOCK_NAMES
        for name, mapped in MAPPED.items():
            measured = rates[name]
            assert abs(measured - mapped) <= tolerance(mapped), \
                (name, measured, mapped)
        # The realization keeps the outputs apart the way the config does.
        assert rates["out1"] > rates["out2"] > rates["out3"] > rates["out0"]
        await session.close()

    asyncio.run(run())
