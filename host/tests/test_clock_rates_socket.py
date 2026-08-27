"""End-to-end test of the clock-measurer instrument over the clock_rates
simulator.

The rack is emitted by ``acrobe gatecap generate`` from
``gateware/example/clock_rates/description.yaml``: an instrument-only rack, one
clock measurer, three observed clocks against a 100 MHz reference, reached over
UDP. The bench runs the host clock at a rate unrelated to the reference, so
every rate the host reads has crossed the instrument's clock boundary.

What is checked here: the descriptor reaching the host intact (the reference
clock, its nominal rate, the update rate and the clock names in register
order), one burst read yielding a rate per clock within the measurement's own
error, the status poll the GUI draws its pill and its curves from, and the CSV
the CLI dumps.

Run: python3.13 -m pytest host/tests/test_clock_rates_socket.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.gui.app import Api
from acrobe_plugin.gatecap.gui.resources import ResourceServer
from acrobe_plugin.gatecap.instrument.clock_measurer.driver import (
    CLOCK_MEASURER_UUID, ClockMeasurer)
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "clock_rates")
RESOURCE = "udp/127.0.0.1:4252/gatecap"

# What description.yaml states.
INSTRUMENT = "rates"
REFERENCE_NAME = "ref"
REFERENCE_HZ = 100_000_000
UPDATE_HZ_L2 = 14
CLOCK_NAMES = ["fast", "slow", "odd"]

# What tb.vhd drives, in the same order: clock periods in seconds.
PERIODS = {"fast": 6e-9, "slow": 125e-9, "odd": 13e-9}
PROGRAMMED = {name: 1.0 / period for name, period in PERIODS.items()}

# Error budget of one measurement, from how the measurement is made.
#
# The instrument counts edges of the observed clock over a window of
# WINDOW_CYCLES = floor(REFERENCE_HZ / 2**UPDATE_HZ_L2) reference cycles and
# publishes (count * 2**UPDATE_HZ_L2) Hz. Two terms follow:
#
#  - the count is an integer, so a rate lands on a multiple of QUANTUM_HZ; and
#    the observed clock's counter is resynchronised into the reference domain,
#    which can move a window boundary by one more edge. Three quanta covers
#    both, generously.
#  - the window is a whole number of reference cycles, hence
#    WINDOW_CYCLES/REFERENCE_HZ seconds rather than exactly
#    2**-UPDATE_HZ_L2 s. That is a pure scale error on every rate --
#    85 ppm with these numbers -- so the budget is relative and sized well
#    above it.
QUANTUM_HZ = 2**UPDATE_HZ_L2
WINDOW_CYCLES = REFERENCE_HZ // 2**UPDATE_HZ_L2
SCALE_PPM = 200


def tolerance(programmed_hz):
    return 3 * QUANTUM_HZ + programmed_hz * SCALE_PPM / 1e6


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/clock_rates"],
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


async def _session():
    session = Session(RESOURCE)
    await session.open()
    return session


def measurer(session):
    blocks = session.blocks_of(ClockMeasurer)
    assert len(blocks) == 1, [b.name for b in session.blocks()]
    return blocks[0]


def test_the_descriptor_reaches_the_host(sim):
    async def run():
        session = await _session()
        instrument = measurer(session)
        assert instrument.name == INSTRUMENT
        assert instrument.envelope.type_uuid == CLOCK_MEASURER_UUID
        # One register file, held by the instrument itself: nothing below it.
        assert list(instrument.children) == []
        assert instrument.reference_name == REFERENCE_NAME
        assert instrument.reference_hz == REFERENCE_HZ
        assert instrument.update_hz_l2 == UPDATE_HZ_L2
        assert instrument.update_hz() == 2**UPDATE_HZ_L2
        assert instrument.quantum_hz() == QUANTUM_HZ
        # Register order, which is what pairs a rate word with a name.
        assert instrument.clock_names == CLOCK_NAMES
        # The instrument answers the fingerprint protocol like any other, so
        # the session seeds itself from it.
        assert await instrument.fingerprint() == session.fingerprint
        await session.close()

    asyncio.run(run())


def test_every_rate_matches_the_clock_the_bench_drives(sim):
    async def run():
        session = await _session()
        instrument = measurer(session)
        rates = await instrument.rates()
        assert list(rates) == CLOCK_NAMES
        for name, programmed in PROGRAMMED.items():
            measured = rates[name]
            assert abs(measured - programmed) <= tolerance(programmed), \
                (name, measured, programmed)
            # And a rate is a multiple of the quantum, as the block advertises.
            assert measured % QUANTUM_HZ == 0, (name, measured)
        # The three clocks are more than a decade apart; a swap or a stuck
        # register would have to survive that.
        assert rates["fast"] > rates["odd"] > rates["slow"]
        await session.close()

    asyncio.run(run())


def test_the_measurement_is_stable_across_reads(sim):
    """Consecutive reads of a free-running measurement land within a couple of
    quanta of each other -- the beat between an observed clock and the window
    is worth one edge either way. A torn crossing would show up as an outlier
    here, since a single flipped bit of the count is up to 2**13 quanta."""
    async def run():
        session = await _session()
        instrument = measurer(session)
        samples = []
        for _ in range(8):
            samples.append(await instrument.rates())
            await asyncio.sleep(0.05)
        for name in CLOCK_NAMES:
            values = [s[name] for s in samples]
            assert max(values) - min(values) <= 2 * QUANTUM_HZ, (name, values)
        await session.close()

    asyncio.run(run())


def test_the_status_poll_carries_the_rates_and_the_pill(sim):
    """What the shell polls once a tick: the fingerprint its change detection
    rides on, the state and tone of the pill, and the rates the pane draws --
    one answer for the whole instrument."""
    async def run():
        session = await _session()
        instrument = measurer(session)
        status = await instrument.poll()
        assert status["fingerprint"] == session.fingerprint
        assert status["state"] == ClockMeasurer.STATE_MEASURING
        assert status["tone"] == "active"
        assert list(status["rates"]) == CLOCK_NAMES
        # The tooltip text names every clock it measured.
        for name in CLOCK_NAMES:
            assert name in status["progress"]
        await session.close()

    asyncio.run(run())


def test_info_cli_describes_the_instrument(sim):
    info = subprocess.run(["acrobe", "gatecap", "-r", RESOURCE, "info"],
                          capture_output=True, text=True, timeout=30)
    assert info.returncode == 0, info.stderr
    out = info.stdout
    assert f"{INSTRUMENT}:" in out
    assert f"reference {REFERENCE_NAME}: 100 MHz nominal" in out
    assert "measured clocks (3): fast, slow, odd" in out
    assert f"refreshed {2**UPDATE_HZ_L2} time(s) per second" in out


def test_rates_cli_dumps_csv(sim, tmp_path):
    out = subprocess.run(["acrobe", "gatecap", "-r", RESOURCE, "rates"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().split("\n")
    assert lines[0] == "clock,rate_hz"
    assert [line.split(",")[0] for line in lines[1:]] == CLOCK_NAMES
    for line in lines[1:]:
        name, value = line.split(",")
        measured, programmed = int(value), PROGRAMMED[name]
        assert abs(measured - programmed) <= tolerance(programmed), line

    # The same read, to a file, addressed by instrument name.
    path = tmp_path / "rates.csv"
    written = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "rates", INSTRUMENT,
         "--output", str(path)],
        capture_output=True, text=True, timeout=30)
    assert written.returncode == 0, written.stderr
    assert f"wrote {path}" in written.stderr
    assert path.read_text().split("\n")[0] == "clock,rate_hz"
    assert len(path.read_text().strip().split("\n")) == 1 + len(CLOCK_NAMES)


def test_rates_cli_rejects_an_unknown_instrument(sim):
    refused = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "rates", "absent"],
        capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert "no clock measurer 'absent'" in refused.stderr
    assert f"available: {INSTRUMENT}" in refused.stderr


def test_the_gui_shows_one_pane_for_the_instrument(sim):
    """The shell's whole seam, headless: the manifest entry it renders a pane
    from, the panel script it loads, the poll that paints the pill and feeds
    the curves, and the one message the pane sends."""
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        resources = ResourceServer()
        api = Api(resources)
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        entry, = res["describe"]["instruments"]
        assert entry["name"] == INSTRUMENT
        assert entry["type"] == str(CLOCK_MEASURER_UUID)
        assert entry["clock_names"] == CLOCK_NAMES
        assert entry["reference_hz"] == REFERENCE_HZ
        assert entry["update_hz"] == 2**UPDATE_HZ_L2
        assert entry["quantum_hz"] == QUANTUM_HZ
        assert entry["key"] and entry["panel_url"].startswith("/r/")

        # The panel script the shell loads, served under the instrument's URL.
        # It registers against the very UUID the manifest routes panes by.
        body, ctype, _ = resources.serve(*_resource_of(entry["panel_url"]))
        assert ctype == "text/javascript"
        assert entry["type"].encode() in body

        status = await api.poll(INSTRUMENT)
        assert status["health"] is True and status["changed"] is False
        assert status["tone"] == "active"
        assert list(status["rates"]) == CLOCK_NAMES

        reply = await api.instrument_message(INSTRUMENT, {"op": "read"})
        assert list(reply["rates"]) == CLOCK_NAMES
        assert (await api.instrument_message(INSTRUMENT, {"op": "arm"}))["error"]
        assert "error" not in await api.disconnect()

    asyncio.run(run())


def _resource_of(url):
    """The (run, owner id, name) triple an id()-addressed resource URL names."""
    _, _, run, owner, name = url.split("/", 4)
    return run, int(owner), name
