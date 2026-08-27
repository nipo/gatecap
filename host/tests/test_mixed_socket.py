"""End-to-end test of a rack holding two instruments of different types, over
the socket_mixed simulator.

The core is emitted by ``acrobe gatecap generate`` from
``gateware/example/socket_mixed/description.yaml``: a logic analyzer on the
clock the transport rides, and a control/status panel on a clock unrelated to
it. The bench loops the panel back on itself -- every control feeds the status
of the same width, every tick output the tick input of the matching name -- so
what the host drives is what it observes.

What is checked here: a mixed rack enumerating both instruments under their own
drivers with one fingerprint, a control reaching the user ports and coming back
as a status (enumeration labels included), a tick strobe showing up as a sticky
bit and a count, write-1-to-clear touching only the bits the host has seen, a
counter rebased by its clear, and a whole word of ticks strobed in one write
stepping every counter of that word -- while the analyzer in the same rack
still captures.

Run: python3.13 -m pytest host/tests/test_mixed_socket.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.gui.app import Api
from acrobe_plugin.gatecap.gui.resources import ResourceServer
from acrobe_plugin.gatecap.instrument.control_status.blocks.registers import \
    PanelRegisters
from acrobe_plugin.gatecap.instrument.control_status.driver import (
    CONTROL_STATUS_UUID, ControlStatusPanel)
from acrobe_plugin.gatecap.instrument.la.driver import LogicAnalyzer
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_mixed")
RESOURCE = "udp/127.0.0.1:4251/gatecap"

CONTROLS = ["led", "level", "mode"]
STATUSES = ["led_echo", "level_echo", "mode_echo"]
TICK_OUT = [["start", "stop"], ["soft_reset"]]
TICK_IN = [["started", "stopped"], ["was_reset"]]
COUNTER_WIDTH = 8


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_mixed"],
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
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


async def _settled(read, expected, tries=200):
    """The loopback runs through two clock crossings on a clock unrelated to
    the transport's, so a value is asked for until it has travelled -- the same
    thing any host reading a status level does."""
    for _ in range(tries):
        value = await read()
        if value == expected:
            return value
    return value


def test_a_mixed_rack_enumerates_both_instruments(sim):
    async def run():
        session = await _session()
        names = [block.name for block in session.blocks()]
        for name in ("la", "main.control", "main.buffer", "main.trigger",
                     "panel", "registers"):
            assert name in names, names

        (analyzer,) = [b for b in session.blocks()
                       if isinstance(b, LogicAnalyzer)]
        (panel,) = [b for b in session.blocks()
                    if isinstance(b, ControlStatusPanel)]
        # Each instrument is bound by its own envelope type, and holds its own
        # children -- the panel's one register file among them.
        assert panel.envelope.type_uuid == CONTROL_STATUS_UUID
        (registers,) = panel.children
        assert isinstance(registers, PanelRegisters)
        assert session.block_by_name("panel.registers") is registers
        assert registers.base == panel.base

        # The inventory the panel drives comes out of the envelope tail alone.
        inventory = panel.inventory
        assert [f.name for f in inventory.controls] == CONTROLS
        assert [f.width for f in inventory.controls] == [1, 12, 2]
        assert [f.name for f in inventory.statuses] == STATUSES
        assert inventory.controls[2].enum == {0: "idle", 1: "run", 2: "test"}
        assert inventory.statuses[2].enum == {0: "idle", 1: "run", 2: "test"}
        assert [[t.name for t in w] for w in inventory.tick_out] == TICK_OUT
        assert [[t.name for t in w] for w in inventory.tick_in] == TICK_IN
        assert inventory.counter_width == COUNTER_WIDTH

        # One instance, one fingerprint, whichever instrument reports it.
        analyzer_control = analyzer.child_controls[0]
        assert await panel.fingerprint() == await analyzer_control.fingerprint()
        assert await panel.fingerprint() == session.fingerprint

        await session.close()

    asyncio.run(run())


def test_a_control_reaches_the_ports_and_comes_back_as_a_status(sim):
    async def run():
        session = await _session()
        panel = session.block_by_name("panel")

        await panel.control_write("led", 1)
        await panel.control_write("level", 0xABC)
        await panel.control_write("mode", "test")     # by enumeration label
        # Read back from the register file: the widths are enforced there.
        assert await panel.controls_read() == {"led": 1, "level": 0xABC,
                                               "mode": 2}
        # And through the bench loopback, on the panel's own clock.
        assert await _settled(lambda: panel.status_read("mode_echo"), 2) == 2
        assert await panel.status_read("led_echo") == 1
        assert await panel.status_read("level_echo") == 0xABC
        assert panel.label("mode_echo", 2) == "test"

        await panel.control_write("mode", 1)
        assert await _settled(lambda: panel.status_read("mode_echo"), 1) == 1
        assert panel.label("mode_echo", 1) == "run"
        # A value the register cannot hold never reaches the wire.
        with pytest.raises(ValueError, match="does not fit"):
            await panel.control_write("level", 1 << 12)
        assert await panel.control_read("level") == 0xABC

        await session.close()

    asyncio.run(run())


def test_ticks_are_counted_cleared_and_rebased(sim):
    async def run():
        session = await _session()
        panel = session.block_by_name("panel")
        await panel.reset("started", "stopped", "was_reset")
        assert await panel.counters_read() == {"started": 0, "stopped": 0,
                                               "was_reset": 0}

        # One tick of one word: one event, one count, one sticky bit.
        await panel.strobe("start")
        counts = await _settled(panel.counters_read,
                                {"started": 1, "stopped": 0, "was_reset": 0})
        assert counts == {"started": 1, "stopped": 0, "was_reset": 0}
        assert await panel.sticky_read() == {"started": True, "stopped": False,
                                             "was_reset": False}

        # A whole word in one write: every tick of it asserts in the same
        # instrument cycle, so every counter of the word steps once.
        await panel.strobe("start", "stop")
        counts = await _settled(panel.counters_read,
                                {"started": 2, "stopped": 1, "was_reset": 0})
        assert counts == {"started": 2, "stopped": 1, "was_reset": 0}
        assert await panel.sticky_read() == {"started": True, "stopped": True,
                                             "was_reset": False}

        # Write-1-to-clear touches exactly the bits the host names.
        await panel.sticky_clear("started")
        assert await panel.sticky_read() == {"started": False, "stopped": True,
                                             "was_reset": False}

        # Clearing a counter rebases that one alone, and the others keep
        # counting from where they were.
        await panel.counters_clear("started")
        assert await panel.counters_read() == {"started": 0, "stopped": 1,
                                               "was_reset": 0}
        await panel.strobe("start")
        counts = await _settled(panel.counters_read,
                                {"started": 1, "stopped": 1, "was_reset": 0})
        assert counts == {"started": 1, "stopped": 1, "was_reset": 0}

        # Ticks of two words cannot fire in one cycle, and the refusal writes
        # nothing at all.
        before = await panel.counters_read()
        with pytest.raises(ValueError, match="cannot fire in one cycle"):
            await panel.strobe("start", "soft_reset")
        assert await panel.counters_read() == before
        # Naming the sequence explicitly does fire both, one word at a time.
        assert await panel.strobe_each("start", "soft_reset") == 2
        counts = await _settled(panel.counters_read,
                                {"started": before["started"] + 1,
                                 "stopped": before["stopped"],
                                 "was_reset": before["was_reset"] + 1})
        assert counts["was_reset"] == before["was_reset"] + 1

        await session.close()

    asyncio.run(run())


def test_the_poll_carries_the_whole_live_panel(sim):
    async def run():
        session = await _session()
        panel = session.block_by_name("panel")
        await panel.reset("started", "stopped", "was_reset")
        await panel.control_write("mode", "idle")
        await _settled(lambda: panel.status_read("mode_echo"), 0)

        quiet = await panel.poll()
        assert quiet["state"] == "idle" and quiet["tone"] == "idle"
        assert quiet["fingerprint"] == session.fingerprint
        assert quiet["counters"] == {"started": 0, "stopped": 0,
                                     "was_reset": 0}
        assert quiet["status"]["mode_echo"] == 0

        await panel.strobe("soft_reset")
        for _ in range(200):
            poll = await panel.poll()
            if poll["sticky"]["was_reset"]:
                break
        assert poll["state"] == "event" and poll["tone"] == "attention"
        assert poll["progress"] == "was_reset"
        assert poll["counters"]["was_reset"] == 1
        # Acknowledging exactly what was seen puts the panel back to idle.
        await panel.sticky_clear("was_reset")
        assert (await panel.poll())["state"] == "idle"

        await session.close()

    asyncio.run(run())


def test_the_analyzer_of_the_mixed_rack_still_captures(sim):
    async def run():
        session = await _session()
        control = session.block_by_name("main.control")
        trigger = control.trigger_node_get()
        value, mask = trigger.ui_adaptor("console").parse_terms(["state=DONE"])
        await trigger.configure(value, mask)
        await control.configure_and_arm(count=16, pretrigger=4)
        triggered, _ = await control.wait_done(tries=2000)
        assert triggered
        result = await control.read_trace(count=16, pretrigger=4)
        window = result["windows"][0]
        assert len(window) == 16
        names = control.signal_names
        state = sum(((window[4] >> names.index(f"state[{i}]")) & 1) << i
                    for i in range(2))
        assert state == 3        # DONE, the sample the capture was cut on
        await session.close()

    asyncio.run(run())


def test_info_describes_both_instruments(sim):
    info = subprocess.run(["acrobe", "gatecap", "-r", RESOURCE, "info"],
                          capture_output=True, text=True, timeout=30)
    assert info.returncode == 0, info.stderr
    out = info.stdout
    assert "control/status panel" in out
    assert "control level: 12 bit(s)" in out
    assert "control mode: 2 bit(s) <0=idle, 1=run, 2=test>" in out
    assert "tick out word 0: start, stop" in out
    assert "tick in word 1: was_reset" in out
    assert "counters: 3, 8 bit(s), wrapping" in out
    # The analyzer of the same rack still describes itself.
    assert "main.control:" in out and "probes (10): state[0]" in out


def test_the_gui_shows_one_panel_per_instrument(sim):
    # The invariant of the shell's top bar: the manifest carries exactly the
    # rack's instruments, in panel order, and never a block. One entry is one
    # pane, one show/hide toggle and one status pill, and every entry has what
    # the shell needs for all three -- a type to route the panel by, a URL to
    # load it from, a settings key, and a poll that answers.
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        api = Api(ResourceServer())
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        instruments = res["describe"]["instruments"]
        assert [i["name"] for i in instruments] == ["la", "panel"]
        assert [i["order"] for i in instruments] == sorted(i["order"]
                                                           for i in instruments)
        for entry in instruments:
            assert entry["type"] and entry["key"]
            assert entry["panel_url"].startswith("/r/")
            status = await api.poll(entry["name"])
            assert status["health"] is True and status["tone"] in (
                "idle", "active", "attention", "error")
            assert status["fingerprint"] == res["describe"]["fingerprint"]

        # Nothing below an instrument reaches the shell: the analyzer's capture
        # domain and trigger, and the panel's register file, are sections of
        # their instrument's panel, none addressable as a pane.
        blocks = ["main.control", "main.trigger", "main.buffer",
                  "la.main.control", "registers", "panel.registers"]
        assert not (set(blocks) & {i["name"] for i in instruments})
        for name in blocks:
            assert (await api.instrument_message(name,
                                                 {"op": "abort"}))["error"]
            assert (await api.poll(name))["error"]
        assert "error" not in await api.disconnect()

    asyncio.run(run())


def test_the_gui_drives_the_panel_beside_the_analyzer(sim):
    # The GUI routes to the panel exactly as to the analyzer: the manifest
    # carries it with its own panel.js, the poll loop addresses it by name and
    # renders its pill from what the driver reports, and every widget action
    # goes through the generic instrument_message router.
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        api = Api(ResourceServer())
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        instruments = res["describe"]["instruments"]
        panel = next(i for i in instruments
                     if i["type"] == str(CONTROL_STATUS_UUID))
        assert panel["name"] == "panel"
        assert panel["panel_url"].startswith("/r/")
        assert [f["name"] for f in panel["controls"]] == CONTROLS
        assert panel["controls"][2]["enum"] == {"0": "idle", "1": "run",
                                                "2": "test"}
        assert panel["tick_out"] == TICK_OUT and panel["tick_in"] == TICK_IN
        assert panel["counter_width"] == COUNTER_WIDTH

        assert "error" not in await api.instrument_message(
            "panel", {"op": "control", "name": "mode", "value": 2})
        values = (await api.instrument_message(
            "panel", {"op": "controls"}))["values"]
        assert values["mode"] == 2

        assert "error" not in await api.instrument_message(
            "panel", {"op": "reset", "names": ["started", "stopped"]})
        assert "error" not in await api.instrument_message(
            "panel", {"op": "tick", "names": ["start", "stop"]})
        status = None
        for _ in range(200):
            status = await api.poll("panel")
            if status.get("sticky", {}).get("started"):
                break
            await asyncio.sleep(0.02)
        # The pill contract: a state the shell prints, a tone it styles, and
        # the fingerprint the rack's change detection rides on.
        assert status["state"] == "event" and status["tone"] == "attention"
        assert status["fingerprint"] == res["describe"]["fingerprint"]
        assert status["changed"] is False and status["health"] is True
        assert status["counters"]["started"] == 1
        assert status["counters"]["stopped"] == 1
        assert status["status"]["mode_echo"] == 2

        # What the pane does with the flash: acknowledge exactly the bits it
        # saw.
        seen = [name for name, pending in status["sticky"].items() if pending]
        assert "error" not in await api.instrument_message(
            "panel", {"op": "ack", "names": seen})
        assert not any((await api.poll("panel"))["sticky"].values())
        assert "error" not in await api.disconnect()

    asyncio.run(run())
