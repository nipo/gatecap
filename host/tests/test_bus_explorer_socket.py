"""End-to-end test of the bus-explorer instrument over the bus_explorer
simulator.

The rack is emitted by ``acrobe gatecap generate`` from
``gateware/example/bus_explorer/description.yaml``: an instrument-only rack,
one bus explorer mastering a 12-bit-address, 32-bit-data target bus on a clock
of its own, eight scan slots, reached over UDP. Behind the target port sits the
stub device ``tests/data/demo_device.svd`` describes -- a read-only identifier,
a control register with three fields, a status register computed from it, a
scratch word mirrored inverted into another address, one address that always
answers pslverr and one that answers far too late.

What is checked here: the descriptor reaching the host intact, reads and writes
and masked writes round-tripping through the target's own clock domain, both
error codes decoding to distinguishable exceptions, the sweep, the scanner's
slots arriving on the status poll the GUI draws its pill from, snapshot and
diff catching a write, the journal recording what the session did and the
recipe replaying it, SVD decode against the checked-in fixture, the CLI verbs
and the GUI seam.

Run: python3.13 -m pytest host/tests/test_bus_explorer_socket.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.gui.app import Api
from acrobe_plugin.gatecap.gui.resources import ResourceServer
from acrobe_plugin.gatecap.instrument.bus_explorer import (BUS_EXPLORER_UUID,
                                                           BusExplorer,
                                                           BusSlaveError,
                                                           BusTimeout,
                                                           MapLibrary)
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "bus_explorer")
DEMO_SVD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                        "demo_device.svd")
RESOURCE = "udp/127.0.0.1:4253/gatecap"

# What description.yaml states.
INSTRUMENT = "dut"
ADDRESS_WIDTH = 12
DATA_WIDTH = 32
SLOT_COUNT = 8
MAP_ID = "gatecap-demo-device"

# The stub device's map, as tb.vhd implements it and the SVD describes it.
ID = 0x000
CTRL = 0x004
STATUS = 0x008
SCRATCH = 0x00C
MIRROR = 0x010
FAULT = 0x020
SLOW = 0x024
ID_VALUE = 0x5CA1AB1E
WORD = 0xFFFFFFFF

# CTRL fields.
ENABLE_BIT = 0x1
MODE_MASK = 0x6
MODE_RUN = 0x2
GAIN_MASK = 0xFF0


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/bus_explorer"],
                   capture_output=True)


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    # The map identifier the descriptor carries resolves against the user's
    # own library, so the suite gets a library of its own rather than writing
    # into the account's real config.
    config = tmp_path_factory.mktemp("gatecap-config")
    os.environ["GATECAP_CONFIG_DIR"] = str(config)
    MapLibrary().add(MAP_ID, DEMO_SVD)

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
        os.environ.pop("GATECAP_CONFIG_DIR", None)


async def _session():
    session = Session(RESOURCE)
    await session.open()
    return session


def explorer(session):
    nodes = session.blocks_of(BusExplorer)
    assert len(nodes) == 1, [b.name for b in session.blocks()]
    return nodes[0]


async def _quiesce(node):
    """Put the target back in its reset state, so each test starts from the
    same device however the previous one left it."""
    await node.scan(False)
    await node.slots_enable(0)
    await node.write(CTRL, 0)
    await node.write(SCRATCH, 0)


def run(body):
    async def wrapped():
        session = await _session()
        node = explorer(session)
        try:
            await _quiesce(node)
            await body(session, node)
        finally:
            await session.close()

    asyncio.run(wrapped())


# -- the descriptor -----------------------------------------------------


def test_the_descriptor_reaches_the_host(sim):
    async def body(session, node):
        assert node.name == INSTRUMENT
        assert node.envelope.type_uuid == BUS_EXPLORER_UUID
        # One child, the engine, at offset 0.
        assert [child.name for child in node.children] == ["engine"]
        assert node.engine.base == node.base
        assert node.address_width == ADDRESS_WIDTH
        assert node.data_width == DATA_WIDTH
        assert node.slot_count == SLOT_COUNT
        assert node.map_id == MAP_ID
        assert node.bus_bytes() == 4
        # The instrument answers the fingerprint protocol like any other, so
        # the session seeds itself from it.
        assert await node.fingerprint() == session.fingerprint

    run(body)


def test_the_map_identifier_resolves_and_names_the_registers(sim):
    async def body(session, node):
        assert node.map is not None and node.map_error is None
        assert len(node.map) == 7
        assert node.name_at(CTRL) == "DEMO.CTRL"
        assert node.name_at(0x014) is None
        # And decode is a pure host-side view of a value the target gave.
        fields = {f["name"]: f
                  for f in node.fields_at(CTRL, ENABLE_BIT | MODE_RUN)}
        assert fields["ENABLE"]["value"] == 1
        assert fields["MODE"]["label"] == "RUN"

    run(body)


# -- one operation at a time ---------------------------------------------


def test_reads_and_writes_round_trip_through_the_target_clock(sim):
    async def body(session, node):
        assert await node.read(ID) == ID_VALUE
        await node.write(SCRATCH, 0xA5A51234)
        assert await node.read(SCRATCH) == 0xA5A51234
        # A second address the write feeds: watching one register react to
        # another is the shape of the work this instrument exists for.
        assert await node.read(MIRROR) == (~0xA5A51234) & WORD
        # A read-only register accepts a write and keeps its value.
        await node.write(ID, 0)
        assert await node.read(ID) == ID_VALUE

    run(body)


def test_a_masked_write_leaves_the_bits_outside_the_mask_alone(sim):
    async def body(session, node):
        await node.write(CTRL, 0x00000AB1)     # ENABLE, MODE=IDLE, GAIN=0xAB
        # The engine does the read-modify-write on the target bus, and hands
        # back the value it read before modifying it.
        before = await node.write_masked(CTRL, MODE_RUN, MODE_MASK)
        assert before == 0x00000AB1
        assert await node.read(CTRL) == 0x00000AB1 | MODE_RUN
        # The device reacts: STATUS is computed from CTRL.
        status = await node.read(STATUS)
        assert status & 0x8, "RUNNING should be set with ENABLE and MODE=RUN"
        assert (status >> 8) & 0xFF == 0xAB
        assert node.fields_at(STATUS, status)[1]["label"] is None

    run(body)


def test_a_field_write_computes_its_mask_out_of_the_map(sim):
    async def body(session, node):
        await node.write(CTRL, 0)
        placed = await node.field_write("CTRL", "MODE", "RUN")
        assert placed == {"address": CTRL, "value": MODE_RUN,
                          "mask": MODE_MASK}
        await node.field_write("DEMO.CTRL", "ENABLE", 1)
        await node.field_write("CTRL", "GAIN", 0x5A)
        assert await node.read(CTRL) == (0x5A << 4) | MODE_RUN | ENABLE_BIT
        assert await node.read(STATUS) == 0x5A00 | 0x8 | MODE_RUN | ENABLE_BIT
        # A read-only register is not offered for writing, and a value that
        # does not fit a field would reach into its neighbours.
        with pytest.raises(ValueError, match="read-only"):
            await node.field_write("STATUS", "RUNNING", 1)
        with pytest.raises(ValueError):
            await node.field_write("CTRL", "MODE", 7)
        with pytest.raises(ValueError, match="no value named"):
            await node.field_write("CTRL", "MODE", "SPIN")

    run(body)


def test_the_two_error_codes_are_distinguishable(sim):
    """A target that refuses and a target that never answers are different
    problems, and the instrument exists so that neither wedges the transport:
    both come back as an exception, and the link is still there afterwards."""
    async def body(session, node):
        with pytest.raises(BusSlaveError):
            await node.read(FAULT)
        with pytest.raises(BusSlaveError):
            await node.write(FAULT, 1)
        # The address that answers long after the engine has given up.
        with pytest.raises(BusTimeout):
            await node.read(SLOW)
        # And the engine is immediately usable again: the late answer was
        # drained, not mistaken for the next command's.
        assert await node.read(ID) == ID_VALUE
        assert await node.read(SCRATCH) == 0

    run(body)


def test_a_sweep_reports_the_holes_instead_of_raising(sim):
    async def body(session, node):
        await node.write(SCRATCH, 0x0F0F0F0F)
        entries = await node.sweep(0, 8)
        assert [e["address"] for e in entries] == list(range(0, 32, 4))
        values = {e["address"]: e["value"] for e in entries}
        errors = {e["address"]: e["error"] for e in entries}
        assert values[ID] == ID_VALUE
        assert values[SCRATCH] == 0x0F0F0F0F
        assert values[MIRROR] == (~0x0F0F0F0F) & WORD
        assert errors[0x014] == errors[0x018] == errors[0x01C] == "slverr"
        assert errors[ID] is None
        # A sweep names what it found, when the map does.
        assert [e["register"] for e in entries[:5]] == \
            ["DEMO.ID", "DEMO.CTRL", "DEMO.STATUS", "DEMO.SCRATCH",
             "DEMO.MIRROR"]

    run(body)


# -- the scanner ---------------------------------------------------------


async def _scanned(node, tries=40):
    """Poll until every enabled slot has a value and the sweep is visibly
    running, the way a frontend does.

    ``scan_active`` reports one live access, not the enable bit, so a status
    read landing between two slot accesses sees it clear. The callers assert on
    the scanning state, so the poll they get back has to be one that caught the
    sweep in the act."""
    for _ in range(tries):
        status = await node.poll()
        if (status["scan_active"]
                and all(entry["valid"] or entry["error"]
                        or not entry["enabled"]
                        for entry in status["scan"])):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("the scanner never filled its slots")


def test_the_slots_ride_the_status_poll(sim):
    async def body(session, node):
        await node.write(CTRL, ENABLE_BIT | MODE_RUN)
        await node.write(SCRATCH, 0x12345678)
        await node.slots_set([ID, CTRL, STATUS, SCRATCH, MIRROR])
        await node.scan(True)
        status = await _scanned(node)

        assert status["scan_active"] is True
        assert status["state"] == BusExplorer.STATE_SCANNING
        assert status["tone"] == "active"
        assert status["fingerprint"] == session.fingerprint
        values = {entry["address"]: entry["value"]
                  for entry in status["scan"] if entry["valid"]}
        assert values[ID] == ID_VALUE
        assert values[CTRL] == ENABLE_BIT | MODE_RUN
        assert values[SCRATCH] == 0x12345678
        assert values[MIRROR] == (~0x12345678) & WORD
        # Each slot is named by the map, which is what makes the table
        # readable.
        assert [e["register"] for e in status["scan"][:5]] == \
            ["DEMO.ID", "DEMO.CTRL", "DEMO.STATUS", "DEMO.SCRATCH",
             "DEMO.MIRROR"]
        # The slots past the ones programmed are disabled and read by nothing.
        assert [e["enabled"] for e in status["scan"][5:]] == [False] * 3

        # A manual write preempts the scanner, and the scanner picks it up.
        await node.write(SCRATCH, 0x0BADF00D)
        for _ in range(40):
            status = await node.poll()
            if status["scan"][3]["value"] == 0x0BADF00D:
                break
            await asyncio.sleep(0.05)
        assert status["scan"][3]["value"] == 0x0BADF00D
        assert status["scan"][4]["value"] == (~0x0BADF00D) & WORD

    run(body)


def test_an_erroring_slot_keeps_its_last_good_value_and_raises_the_pill(sim):
    async def body(session, node):
        await node.write(SCRATCH, 0xCAFEBABE)
        await node.slots_set([SCRATCH, ID])
        await node.scan(True)
        status = await _scanned(node)
        assert status["scan"][0]["value"] == 0xCAFEBABE
        assert status["tone"] == "active"

        # Point the slot at the address that always refuses. Reprogramming it
        # clears its flags, so the error is the answer to the new question.
        await node.slot_set(0, FAULT)
        for _ in range(40):
            status = await node.poll()
            if status["scan"][0]["error"]:
                break
            await asyncio.sleep(0.05)
        assert status["scan"][0]["error"] is True
        assert status["scan"][0]["valid"] is False
        assert status["state"] == BusExplorer.STATE_ERROR
        assert status["tone"] == "attention"
        assert "DEMO.FAULT" in status["progress"]
        # The good slot beside it is untouched.
        assert status["scan"][1]["value"] == ID_VALUE

    run(body)


# -- snapshots -----------------------------------------------------------


def test_a_snapshot_pair_catches_the_write_between_them(sim):
    async def body(session, node):
        await node.write(CTRL, 0)
        await node.write(SCRATCH, 0x11112222)
        before = await node.snapshot("before", start=0, count=5)
        assert before.values[SCRATCH] == 0x11112222
        assert before.errors == {}

        await node.field_write("CTRL", "MODE", "TEST")
        await node.write(SCRATCH, 0x11119999)
        await node.snapshot("after", start=0, count=5)

        changes = {c["address"]: c for c in node.diff("before", "after")}
        assert set(changes) == {CTRL, STATUS, SCRATCH, MIRROR}
        assert changes[SCRATCH]["before"] == 0x11112222
        assert changes[SCRATCH]["after"] == 0x11119999
        assert changes[CTRL]["register"] == "DEMO.CTRL"
        # A diff of a mapped register names the fields that moved.
        assert changes[CTRL]["fields"] == ["MODE"]
        assert changes[STATUS]["fields"] == ["MODE"]
        # ID did not move, so it is not in the diff at all.
        assert ID not in changes

        # A snapshot naming neither a range nor addresses takes the slots.
        await node.slots_set([ID, SCRATCH])
        slots = await node.snapshot("slots")
        assert slots.addresses() == [ID, SCRATCH]
        # And an address that refuses is recorded as an error, not a value.
        errored = await node.snapshot("hole", addresses=[SCRATCH, FAULT])
        assert errored.values == {SCRATCH: 0x11119999}
        assert errored.errors == {FAULT: "slverr"}

    run(body)


# -- the journal ---------------------------------------------------------


def test_the_journal_records_the_session_and_replays_it(sim):
    async def body(session, node):
        node.journal.clear()
        await node.write(SCRATCH, 0xDEADBEEF)
        await node.write_masked(CTRL, ENABLE_BIT, ENABLE_BIT)
        await node.field_write("CTRL", "MODE", "RUN")
        await node.field_write("CTRL", "GAIN", 0x33)
        # A read passes through the same hook and is counted, not recorded:
        # the journal is what the session changed.
        assert await node.read(ID) == ID_VALUE

        assert len(node.journal) == 4 and node.journal.reads > 0
        entries = node.journal.entries
        assert [e.op for e in entries] == \
            ["write", "masked-write", "masked-write", "masked-write"]
        assert entries[0].decoded() == "DEMO.SCRATCH"
        assert entries[2].decoded() == "DEMO.CTRL.MODE"
        assert entries[2].mask == MODE_MASK
        listing = node.journal.listing()
        assert "DEMO.CTRL.MODE" in listing and "DEMO.SCRATCH" in listing

        recipe = node.recipe()
        assert recipe["map"] == MAP_ID
        assert len(recipe["steps"]) == 4
        final = {CTRL: await node.read(CTRL),
                 SCRATCH: await node.read(SCRATCH),
                 STATUS: await node.read(STATUS)}
        assert final[CTRL] == (0x33 << 4) | MODE_RUN | ENABLE_BIT

        # Wipe the device and replay: the recipe is the artifact the session
        # exists to produce, so it has to reproduce the state.
        await node.write(CTRL, 0)
        await node.write(SCRATCH, 0)
        node.journal.clear()
        assert await node.replay(recipe) == 4
        assert {CTRL: await node.read(CTRL),
                SCRATCH: await node.read(SCRATCH),
                STATUS: await node.read(STATUS)} == final
        # Replaying a session is a session too.
        assert len(node.journal) == 4

    run(body)


def test_a_failing_step_stops_the_replay_where_it_failed(sim):
    async def body(session, node):
        recipe = {"gatecap-bus-explorer-recipe": 1, "instrument": INSTRUMENT,
                  "map": None,
                  "steps": [{"op": "write", "address": SCRATCH, "value": 7},
                            {"op": "write", "address": FAULT, "value": 1},
                            {"op": "write", "address": SCRATCH, "value": 9}]}
        with pytest.raises(RuntimeError, match="recipe step 1"):
            await node.replay(recipe)
        assert await node.read(SCRATCH) == 7

    run(body)


# -- the CLI -------------------------------------------------------------


def _cli(*args, timeout=60):
    return subprocess.run(["acrobe", "gatecap", *args], capture_output=True,
                          text=True, timeout=timeout, env=dict(os.environ))


def test_info_cli_describes_the_instrument(sim):
    info = _cli("-r", RESOURCE, "info")
    assert info.returncode == 0, info.stderr
    assert f"{INSTRUMENT}:" in info.stdout
    assert (f"bus explorer: {ADDRESS_WIDTH} address bit(s), "
            f"{DATA_WIDTH} data bit(s), {SLOT_COUNT} scan slot(s)") \
        in info.stdout
    assert f"map {MAP_ID}: 7 register(s)" in info.stdout


def test_bus_cli_reads_writes_and_dumps(sim, tmp_path):
    out = _cli("-r", RESOURCE, "bus", "read", "0x0")
    assert out.returncode == 0, out.stderr
    assert "0x0: 0x5ca1ab1e" in out.stdout and "DEMO.ID" in out.stdout

    written = _cli("-r", RESOURCE, "bus", "write", "0xc", "0x5555aaaa")
    assert written.returncode == 0, written.stderr
    assert "DEMO.SCRATCH" in written.stderr
    back = _cli("-r", RESOURCE, "bus", "read", "0xc")
    assert "0x5555aaaa" in back.stdout

    # A masked write, and the field decode a read prints under the value.
    masked = _cli("-r", RESOURCE, "bus", "write", "0x4", "0x2", "--mask", "0x6")
    assert masked.returncode == 0, masked.stderr
    ctrl = _cli("-r", RESOURCE, "bus", "read", "0x4")
    assert "[2:1] MODE = 0x1 (RUN)" in ctrl.stdout

    # A field by name, whose mask the map computes.
    field = _cli("-r", RESOURCE, "bus", "field", "CTRL", "MODE", "TEST")
    assert field.returncode == 0, field.stderr
    assert "mask 0x6" in field.stderr
    assert "[2:1] MODE = 0x2 (TEST)" in _cli("-r", RESOURCE, "bus", "read",
                                             "0x4").stdout

    dump = _cli("-r", RESOURCE, "bus", "dump", "0", "8")
    assert dump.returncode == 0, dump.stderr
    lines = dump.stdout.strip().split("\n")
    assert lines[0] == "address,register,value,error"
    assert lines[1] == "0x0,DEMO.ID,0x5ca1ab1e,"
    assert lines[6].endswith(",slverr")
    assert len(lines) == 9

    path = tmp_path / "regs.csv"
    to_file = _cli("-r", RESOURCE, "bus", "dump", "0", "4", "--output",
                   str(path))
    assert to_file.returncode == 0, to_file.stderr
    assert f"wrote {path}" in to_file.stderr
    assert len(path.read_text().strip().split("\n")) == 5


def test_bus_cli_reports_a_refusing_target(sim):
    refused = _cli("-r", RESOURCE, "bus", "read", "0x20")
    assert refused.returncode != 0
    assert "pslverr" in refused.stderr
    unknown = _cli("-r", RESOURCE, "bus", "read", "0x0", "-i", "absent")
    assert unknown.returncode != 0
    assert "no bus explorer 'absent'" in unknown.stderr


def test_bus_map_cli_lists_what_the_session_registered(sim):
    listed = _cli("bus", "map", "list")
    assert listed.returncode == 0, listed.stderr
    assert MAP_ID in listed.stdout and "demo_device.svd" in listed.stdout


# -- the GUI seam --------------------------------------------------------


def test_the_gui_shows_one_pane_for_the_instrument(sim):
    """The shell's whole seam, headless: the manifest entry it renders a pane
    from, the panel script it loads, the poll that paints the pill and the
    register table, and the ops the pane sends."""
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        resources = ResourceServer()
        api = Api(resources)
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        entry, = res["describe"]["instruments"]
        assert entry["name"] == INSTRUMENT
        assert entry["type"] == str(BUS_EXPLORER_UUID)
        assert entry["address_width"] == ADDRESS_WIDTH
        assert entry["data_width"] == DATA_WIDTH
        assert entry["slot_count"] == SLOT_COUNT
        assert entry["map_id"] == MAP_ID
        assert entry["key"] and entry["panel_url"].startswith("/r/")

        # The panel script the shell loads, served under the instrument's URL.
        # It registers against the very UUID the manifest routes panes by.
        body, ctype, _ = resources.serve(*_resource_of(entry["panel_url"]))
        assert ctype == "text/javascript"
        assert entry["type"].encode() in body

        async def send(msg):
            return await api.instrument_message(INSTRUMENT, msg)

        # The map does not ride the manifest: the panel key hashes the
        # description and must follow the gateware, not a document the user
        # registered.
        assert "registers" not in entry
        map_reply = await send({"op": "map"})
        assert map_reply["loaded"] is True and len(map_reply["registers"]) == 7
        assert map_reply["registers"][1]["name"] == "DEMO.CTRL"
        assert [f["name"] for f in map_reply["registers"][1]["fields"]] == \
            ["ENABLE", "MODE", "GAIN"]
        assert map_reply["registers"][0]["writable"] is False

        assert (await send({"op": "write", "address": SCRATCH,
                            "value": 0x1234}))["ok"]
        read = await send({"op": "read", "address": SCRATCH})
        assert read["value"] == 0x1234 and read["register"] == "DEMO.SCRATCH"
        assert (await send({"op": "read", "address": FAULT}))["error"]

        assert (await send({"op": "field", "register": "CTRL",
                            "field": "MODE", "value": 1}))["ok"]
        ctrl = await send({"op": "read", "address": CTRL})
        assert [f["label"] for f in ctrl["fields"]][1] == "RUN"
        assert (await send({"op": "decode", "address": CTRL,
                            "value": 0}))["fields"][1]["label"] == "IDLE"

        assert (await send({"op": "slots_set",
                            "addresses": [ID, CTRL, SCRATCH]}))["ok"]
        assert (await send({"op": "scan", "enabled": True}))["ok"]
        slots = await send({"op": "slots"})
        assert len(slots["slots"]) == SLOT_COUNT
        assert slots["slots"][0]["address"] == ID
        assert (await send({"op": "slot_enable", "index": 2,
                            "enabled": False}))["ok"]

        status = await api.poll(INSTRUMENT)
        assert status["health"] is True and status["changed"] is False
        assert status["tone"] in ("active", "idle", "attention")
        assert len(status["scan"]) == SLOT_COUNT
        assert "progress" in status and "state" in status

        journal = await send({"op": "journal"})
        assert len(journal["entries"]) >= 2
        assert "DEMO.CTRL.MODE" in journal["text"]
        recipe = await send({"op": "recipe"})
        assert recipe["recipe"]["map"] == MAP_ID
        assert (await send({"op": "replay",
                            "recipe": recipe["recipe"]}))["steps"] >= 2
        assert (await send({"op": "journal_clear"}))["ok"]
        assert (await send({"op": "journal"}))["entries"] == []

        assert (await send({"op": "arm"}))["error"]
        assert "error" not in await api.disconnect()

    asyncio.run(run())


def _resource_of(url):
    """The (run, owner id, name) triple an id()-addressed resource URL names."""
    _, _, run, owner, name = url.split("/", 4)
    return run, int(owner), name
