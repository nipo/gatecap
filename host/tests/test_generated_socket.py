"""End-to-end test of a generated two-domain capture core with one shared
trigger, over the socket_generated simulator.

The core is emitted by ``acrobe gatecap generate`` from
``gateware/example/socket_generated/description.yaml``: a control domain at
100 MHz probing an AXI4-Stream, a state bus and a counter, and a phy domain at
125 MHz probing its own counter plus a mark bit driven from the control
domain's trigger condition. The phy capture subscribes to the control domain's
trigger.

What is checked here: the domain-prefixed enumeration and the analyzer's
resolved member controls, the name-spec and enum tables surviving into the host,
the ready-AND that keeps the shared trigger disabled while any subscriber is
unarmed, and the correlation of the two windows -- the control window's trigger
sample is the matched cycle, and the phy window's is the same instant in
absolute time, which only holds if the subscriber back-dates by its integration
latency as well as by the trigger's own.

Then the same core driven as one group through the analyzer: group arm and
abort over both domains, the composed group state, and the single absolute-time
VCD the group reads back -- one scope per domain, each domain on its own exact
sample period, both aligned on the trigger.

Run: python3.13 -m pytest host/tests/test_generated_socket.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.instrument.la.driver import LogicAnalyzer
from acrobe_plugin.gatecap.instrument.la.blocks.control import Control
from acrobe_plugin.gatecap.gui.app import Api
from acrobe_plugin.gatecap.gui.resources import ResourceServer
from acrobe_plugin.gatecap.session import Session

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_generated")
RESOURCE = "udp/127.0.0.1:4248/gatecap"

COUNT = 32
PRETRIGGER = 8
# The same capture asked for as a window in real time. Both domains cover it,
# each at its own rate: 2 µs is 200 samples at 100 MHz and 250 at 125 MHz, of
# which 400 ns (40 and 50 samples) precede the trigger.
SPAN = 2e-6
PRE_SPAN = 400e-9
SPAN_COUNTS = {"control.control": (200, 40), "phy.control": (250, 50)}
# Control cycles between two trigger conditions, as the bench generates them.
EVENT_PERIOD = 64
# What the descriptor must report for each domain: the hosting domain is wired
# straight to its trigger, the subscriber sits behind one interdomain_tick.
HOST_LATENCY = 0
SUBSCRIBER_LATENCY = 3

CONTROL_NAMES = ([f"command.data[{i}]" for i in range(8)]
                 + ["command.valid", "command.last", "command.ready"]
                 + [f"state[{i}]" for i in range(2)]
                 + [f"count[{i}]" for i in range(8)])
PHY_NAMES = [f"word[{i}]" for i in range(8)] + ["mark"]
STATE_ENUM = {0: "IDLE", 1: "BUSY", 2: "HOLD", 3: "DONE"}
STATE_BUSY = 1
STATE_DONE = 3


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_generated"],
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


class Vector:
    """Bit-field access over a capture sample, indexed by the probe names the
    descriptor advertises -- so a layout change shows up as a name lookup
    failure rather than as silently shifted values."""

    def __init__(self, names):
        self.index = {name: bit for bit, name in enumerate(names)}

    def bit(self, sample, name):
        return (sample >> self.index[name]) & 1

    def bus(self, sample, name, width):
        return sum(self.bit(sample, f"{name}[{i}]") << i for i in range(width))


class Scenario:
    """One run of the shared-trigger sequence against the simulator."""

    # Wall-clock budget of the negative check: the trigger condition recurs
    # every EVENT_PERIOD control cycles, so this window holds a very large
    # number of them.
    UNARMED_SECONDS = 1.0
    UNARMED_POLLS = 20

    def __init__(self, session):
        self.session = session
        self.control = session.block_by_name("control.control")
        self.phy = session.block_by_name("phy.control")
        self.trigger = self.control.trigger_node_get()
        self.host_states = set()
        self.subscriber_states = set()
        self.unarmed_seconds = 0.0
        self.fire_seconds = 0.0

    async def arm_trigger(self):
        """Program the shared trigger through the console term parser, so the
        enum label the descriptor carries is what selects the match."""
        console = self.trigger.ui_adaptor("console")
        value, mask = console.parse_terms(["state=DONE"])
        await self.trigger.configure(value, mask)
        return value, mask

    async def arm_host_only(self):
        """Arm the hosting domain's control and watch the trigger condition go
        by without firing: the shared trigger is the AND of every core's ready,
        and the subscriber is still idle."""
        await self.control.configure_and_arm(count=COUNT, pretrigger=PRETRIGGER)
        start = time.monotonic()
        for _ in range(self.UNARMED_POLLS):
            state, triggered, windows = await self.control.status()
            self.host_states.add((state, triggered, windows))
            self.subscriber_states.add((await self.phy.status())[0])
            await asyncio.sleep(self.UNARMED_SECONDS / self.UNARMED_POLLS)
        self.unarmed_seconds = time.monotonic() - start

    async def arm_subscriber(self):
        """Arm the subscriber; both cores are now ready, so the next condition
        cuts both buffers."""
        start = time.monotonic()
        await self.phy.configure_and_arm(count=COUNT, pretrigger=PRETRIGGER)
        for _ in range(500):
            control = await self.control.status()
            phy = await self.phy.status()
            if control[0] == Control.STATE_IDLE and phy[0] == Control.STATE_IDLE:
                break
        else:
            raise TimeoutError(f"capture never completed (control={control}, "
                               f"phy={phy})")
        self.fire_seconds = time.monotonic() - start
        return control, phy

    async def windows(self):
        control = await self.control.read_trace(count=COUNT,
                                                pretrigger=PRETRIGGER)
        phy = await self.phy.read_trace(count=COUNT, pretrigger=PRETRIGGER)
        return control["windows"][0], phy["windows"][0]


async def _session():
    session = Session(RESOURCE)
    await session.open()
    return session


def test_enumeration_exposes_domains_under_their_analyzer(sim):
    async def run():
        session = await _session()
        names = [b.name for b in session.blocks()]
        for block in ("control.control", "control.buffer", "control.trigger",
                      "phy.control", "phy.buffer", "la"):
            assert block in names, names
        # The phy domain has no trigger block of its own: it subscribes.
        assert "phy.trigger" not in names

        (analyzer,) = [b for b in session.blocks()
                       if isinstance(b, LogicAnalyzer)]
        # Every block of the analyzer hangs under it, addressed through it.
        assert {c.name for c in analyzer.children} == {
            "control.control", "control.buffer", "control.trigger",
            "phy.control", "phy.buffer"}
        assert session.block_by_name("la.control.control") is \
            session.block_by_name("control.control")
        children = analyzer.child_controls
        assert [c.name for c in children] == ["control.control", "phy.control"]
        assert all(isinstance(c, Control) for c in children)

        control, phy = children
        assert control.integration_latency == HOST_LATENCY
        assert phy.integration_latency == SUBSCRIBER_LATENCY
        assert (control.sample_rate, phy.sample_rate) == (100_000_000,
                                                          125_000_000)
        # Both domains name the same trigger block: one event, two buffers.
        assert control.trigger == phy.trigger == "control.trigger"
        assert (control.sink, phy.sink) == ("control.buffer", "phy.buffer")

        # The group orchestrates through the one trigger its members share.
        assert analyzer.trigger_node_get() is control.trigger_node_get()

        await session.close()

    asyncio.run(run())


def test_names_and_enums_survive_to_the_host(sim):
    async def run():
        session = await _session()
        control = session.block_by_name("control.control")
        phy = session.block_by_name("phy.control")

        # Stream field names come out of the packer at elaboration, from the
        # config generic the bench passes in.
        assert control.signal_names == CONTROL_NAMES
        assert control.signal_count == len(CONTROL_NAMES)
        assert phy.signal_names == PHY_NAMES
        assert control.signal_enums == {"state": STATE_ENUM}
        assert phy.signal_enums == {}

        trigger = control.trigger_node_get()
        assert trigger.signal_names == ["command.valid", "command.last",
                                        "state[0]", "state[1]"]
        assert trigger.signal_enums == {"state": STATE_ENUM}
        # state is the third and fourth trigger bits, DONE is 0b11.
        value, mask = trigger.ui_adaptor("console").parse_terms(["state=DONE"])
        assert (value, mask) == (0xC, 0xC)

        await session.close()

    asyncio.run(run())


def test_info_cli_describes_the_group(sim):
    info = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "info"],
        capture_output=True, text=True, timeout=30)
    assert info.returncode == 0, info.stderr
    out = info.stdout
    assert "correlated capture group of 2 control(s)" in out
    assert (f"member control.control: 21 probes, sample clock 100 MHz, "
            f"trigger integration latency {HOST_LATENCY} cycle(s)") in out
    assert (f"member phy.control: 9 probes, sample clock 125 MHz, "
            f"trigger integration latency {SUBSCRIBER_LATENCY} cycle(s)") in out


def test_shared_trigger_correlates_both_domains(sim):
    async def run():
        session = await _session()
        scenario = Scenario(session)
        await scenario.arm_trigger()

        await scenario.arm_host_only()
        # The hosting core stayed armed and untriggered for the whole window,
        # and the subscriber stayed idle.
        assert scenario.host_states == {(Control.STATE_ARMED, False, 0)}, \
            scenario.host_states
        assert scenario.subscriber_states == {Control.STATE_IDLE}, \
            scenario.subscriber_states

        control_status, phy_status = await scenario.arm_subscriber()
        assert control_status == (Control.STATE_IDLE, True, 1)
        assert phy_status == (Control.STATE_IDLE, True, 1)
        # Arming the subscriber is what fired it: the capture completed in a
        # small fraction of the window the condition went by unserved.
        assert scenario.fire_seconds * 10 < scenario.unarmed_seconds

        control_window, phy_window = await scenario.windows()
        assert len(control_window) == COUNT and len(phy_window) == COUNT

        control_bits = Vector(CONTROL_NAMES)
        phy_bits = Vector(PHY_NAMES)

        # Control domain: the trigger sample is the matched cycle (state DONE,
        # counter on an event boundary), every other sample is BUSY, and the
        # counter runs consecutively across the window.
        trigger_count = control_bits.bus(control_window[PRETRIGGER], "count", 8)
        assert trigger_count % EVENT_PERIOD == 0
        for i, sample in enumerate(control_window):
            state = control_bits.bus(sample, "state", 2)
            count = control_bits.bus(sample, "count", 8)
            assert state == (STATE_DONE if i == PRETRIGGER else STATE_BUSY), i
            assert count == (trigger_count - PRETRIGGER + i) % 256, i
            # The probed stream carries the counter, valid every other cycle,
            # last on every sixteenth value.
            assert control_bits.bus(sample, "command.data", 8) == count, i
            assert control_bits.bit(sample, "command.valid") == count % 2, i
            assert control_bits.bit(sample, "command.last") == (
                1 if count % 16 == 15 else 0), i
            assert control_bits.bit(sample, "command.ready") == 1, i

        # Phy domain: its own counter, consecutive across the window.
        first_word = phy_bits.bus(phy_window[0], "word", 8)
        for i, sample in enumerate(phy_window):
            assert phy_bits.bus(sample, "word", 8) == (first_word + i) % 256, i

        # The mark bit is the control-domain event seen from the phy clock: a
        # single run of samples (the event lasts one 10 ns control cycle, so it
        # covers two 8 ns phy samples), ending right at the phy window's
        # trigger sample. A subscriber back-dating by the trigger's own latency
        # alone would put the trigger sample SUBSCRIBER_LATENCY cycles further
        # on, leaving the marks well before it.
        marks = [i for i, s in enumerate(phy_window)
                 if phy_bits.bit(s, "mark")]
        assert marks, "the control-domain event never reached the phy probes"
        assert marks == list(range(marks[0], marks[-1] + 1)), marks
        assert 0 <= PRETRIGGER - marks[-1] <= 2, (marks, PRETRIGGER)
        assert PRETRIGGER - marks[0] <= 2, (marks, PRETRIGGER)

        await session.close()

    asyncio.run(run())


class Vcd:
    """Just enough of a VCD reader for the structural checks: the timescale,
    the declared scope paths, and every variable's changes by dotted name."""

    def __init__(self, text):
        self.timescale = None
        self.scopes = set()
        self.changes = {}          # dotted var name -> [(timestamp, value)]
        ids = {}                   # vcd identifier -> dotted var name
        path = []
        time = 0
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "$timescale":
                self.timescale = " ".join(parts[1:-1])
            elif parts[0] == "$scope":
                path.append(parts[2])
                self.scopes.add(".".join(path))
            elif parts[0] == "$upscope":
                path.pop()
            elif parts[0] == "$var":
                # $var <type> <size> <id> <name> $end
                name = ".".join(path + [parts[4]])
                ids[parts[3]] = name
                self.changes.setdefault(name, [])
            elif parts[0].startswith("#"):
                time = int(parts[0][1:])
            elif parts[0][0] in "sb" and len(parts) == 2:
                self.changes[ids[parts[1]]].append((time, parts[0][1:]))
            elif parts[0][0] in "01xz" and len(parts[0]) > 1:
                self.changes[ids[parts[0][1:]]].append((time, parts[0][0]))

    def timestamps(self, prefix):
        """Every timestamp at which a variable under `prefix` changed."""
        return sorted({t for name, changes in self.changes.items()
                       if name.startswith(prefix) for t, _ in changes})


class Group:
    """One run of the shared-trigger sequence driven through the analyzer."""

    # A compare no sample can satisfy: the bench asserts the event (state DONE)
    # on an even counter that is not an end-of-packet, so valid and last are
    # both low there.
    NEVER = (0xF, 0xF)

    def __init__(self, session):
        self.session = session
        (self.analyzer,) = [b for b in session.blocks()
                            if isinstance(b, LogicAnalyzer)]
        self.control, self.phy = self.analyzer.child_controls

    def terms(self, *terms):
        console = self.analyzer.trigger_node_get().ui_adaptor("console")
        return console.parse_terms(list(terms))

    async def settle(self, state, tries=500):
        for _ in range(tries):
            status = await self.analyzer.status()
            if status[0] == state:
                return status
        raise TimeoutError(f"group never reached state {state} ({status})")


def test_group_arm_composes_one_absolute_time_vcd(sim, tmp_path):
    async def run():
        session = await _session()
        group = Group(session)

        # One call programs the shared trigger and arms both domains over one
        # window in real time; the ready-AND makes the arm order irrelevant.
        plan = await group.analyzer.configure_and_arm(
            trigger=group.terms("state=DONE"), seconds=SPAN,
            pre_seconds=PRE_SPAN)
        # Each member converted the window with its own capture clock, and
        # neither had to be clamped.
        assert {m.name: (m.params["count"], m.params["pretrigger"])
                for m in plan} == SPAN_COUNTS
        assert plan.notes == []
        assert await group.settle(Control.STATE_IDLE) == (Control.STATE_IDLE,
                                                          True, 1)
        for control in group.analyzer.child_controls:
            assert await control.status() == (Control.STATE_IDLE, True, 1)

        # A read with no arguments reuses what the group arm resolved.
        result = await group.analyzer.read_trace()
        assert result["kind"] == "group"
        assert [m["name"] for m in result["members"]] == ["control.control",
                                                          "phy.control"]

        composed = group.analyzer.compose(result)
        # 1 ps places both 10 ns and 8 ns exactly, so no instant is rounded.
        assert composed.timebase.timescale == "1 ps"
        assert composed.periods == [10_000, 8_000]

        ticks = {index: [t for t, member, _ in composed.events if member == index]
                 for index in (0, 1)}
        counts = [SPAN_COUNTS[name][0] for name in ("control.control",
                                                     "phy.control")]
        pres = [SPAN_COUNTS[name][1] for name in ("control.control",
                                                   "phy.control")]
        for index, period in ((0, 10_000), (1, 8_000)):
            assert len(ticks[index]) == counts[index]
            assert all(b - a == period
                       for a, b in zip(ticks[index], ticks[index][1:]))
        # Both domains' trigger sample is the one instant the group is aligned
        # on, and the earliest sample of the capture is the file's zero.
        trigger_tick = composed.trigger_ticks[0]
        assert ticks[0][pres[0]] == ticks[1][pres[1]] == trigger_tick
        assert min(ticks[0][0], ticks[1][0]) == 0
        # Both domains cover the window that was asked for, to within one
        # sample of the slowest of them -- which is what a window in real time
        # buys over a shared sample count.
        spans = [ticks[index][-1] - ticks[index][0] for index in (0, 1)]
        assert abs(spans[0] - spans[1]) <= 10_000
        assert all(abs(span - SPAN * 1e12) <= 10_000 for span in spans)

        # The control domain's trigger sample is the matched condition itself.
        control_window = result["members"][0]["result"]["windows"][0]
        assert Vector(CONTROL_NAMES).bus(control_window[pres[0]],
                                         "state", 2) == STATE_DONE

        vcd = Vcd(group.analyzer.ui_adaptor("console").render(result, "vcd")
                  .decode())
        assert vcd.timescale == "1 ps"
        # One branch per member, under the shared capture root.
        assert {"capture", "capture.control", "capture.control.control",
                "capture.phy", "capture.phy.control"} <= vcd.scopes
        # Enum labels survive the composition, on the trigger sample.
        assert (trigger_tick, "DONE") in vcd.changes["capture.control.control.state"]
        assert any(v == "BUSY"
                   for _, v in vcd.changes["capture.control.control.state"])
        # Each domain's own sample grid, exactly, both phased on the trigger.
        bounds = {}
        for prefix, period in (("capture.control.control.", 10_000),
                               ("capture.phy.control.", 8_000)):
            stamps = vcd.timestamps(prefix)
            assert stamps, prefix
            assert all((t - trigger_tick) % period == 0 for t in stamps), prefix
            bounds[prefix] = (stamps[0], stamps[-1])
        # Both scopes cover the same stretch of real time, to within one sample
        # of the slowest domain -- the counters change every cycle, so the
        # first and last events of a scope are its window's edges.
        (a0, a1), (b0, b1) = bounds.values()
        assert abs(a0 - b0) <= 10_000 and abs(a1 - b1) <= 10_000
        # The phy domain's mark -- the control-domain event seen from the phy
        # clock -- lands on the phy samples right before its trigger sample.
        # The bench asserts the condition periodically, so a window this long
        # holds several marks; the last one before the trigger is the event the
        # capture was cut on.
        marks = [t for t, v in vcd.changes["capture.phy.control.mark"] if v == "1"]
        before = [t for t in marks if t <= trigger_tick]
        assert before and 0 <= trigger_tick - max(before) <= 2 * 8_000

        await session.close()

    asyncio.run(run())


def test_group_state_composes_and_abort_returns_every_member_to_idle(sim):
    async def run():
        session = await _session()
        group = Group(session)
        await group.analyzer.trigger_node_get().configure(*Group.NEVER)

        # Half-armed: the group reports the busiest member's state, so it is
        # armed as a whole while the subscriber is still idle.
        await group.control.configure_and_arm(count=COUNT, pretrigger=PRETRIGGER)
        assert await group.analyzer.status() == (Control.STATE_ARMED, False, 0)
        assert (await group.phy.status())[0] == Control.STATE_IDLE

        # Arming the group brings the other member up; the compare cannot fire,
        # so both stay armed.
        await group.analyzer.configure_and_arm(seconds=SPAN,
                                                pre_seconds=PRE_SPAN)
        assert await group.analyzer.status() == (Control.STATE_ARMED, False, 0)
        for control in group.analyzer.child_controls:
            assert (await control.status())[0] == Control.STATE_ARMED

        await group.analyzer.abort()
        assert await group.settle(Control.STATE_IDLE) == (Control.STATE_IDLE,
                                                          False, 0)
        for control in group.analyzer.child_controls:
            assert (await control.status())[0] == Control.STATE_IDLE

        await session.close()

    asyncio.run(run())


def test_capture_cli_drives_the_group(sim, tmp_path):
    out = tmp_path / "group.vcd"
    run = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "capture", "la",
         "--trigger", "state=DONE", "--span", "2us", "--pre", "400ns",
         "--timeout", "20", "--output", str(out)],
        capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert "captured (vcd)" in run.stderr
    # The window each member derived is reported before the capture runs.
    for name, (count, pre) in SPAN_COUNTS.items():
        assert f"{name}: {count} samples" in run.stderr
        assert f"{pre} pre-trigger" in run.stderr
    vcd = Vcd(out.read_text())
    assert vcd.timescale == "1 ps"
    assert {"capture.control.control", "capture.phy.control"} <= vcd.scopes

    # A format that cannot hold two timebases fails outright, with no file.
    csv = tmp_path / "group.csv"
    refused = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "capture", "la",
         "--trigger", "state=DONE", "--span", "2us", "--pre", "400ns",
         "--timeout", "20", "--format", "csv", "--output", str(csv)],
        capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0
    assert "one timebase per member" in refused.stderr
    assert not csv.exists()


def test_capture_cli_refuses_sample_counts_on_a_group(sim, tmp_path):
    # A group's members sample at different rates, so a shared sample count
    # means nothing; the refusal says what to use instead, and nothing is armed.
    out = tmp_path / "counted.vcd"
    refused = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "capture", "la",
         "--trigger", "state=DONE", "--count", str(COUNT),
         "--pretrigger", str(PRETRIGGER), "--output", str(out)],
        capture_output=True, text=True, timeout=60)
    assert refused.returncode != 0
    assert "--span/--pre" in refused.stderr and not out.exists()

    # The converse: a single control is captured in its own samples.
    refused = subprocess.run(
        ["acrobe", "gatecap", "-r", RESOURCE, "capture", "control.control",
         "--trigger", "state=DONE", "--span", "2us", "--output", str(out)],
        capture_output=True, text=True, timeout=60)
    assert refused.returncode != 0
    assert "--count/--pretrigger" in refused.stderr and not out.exists()


def test_gui_api_drives_the_group(sim):
    # The GUI drives the whole analyzer through one panel: the manifest carries
    # the instrument alone -- its trigger and its capture domains are sections
    # of that panel, not entries of their own -- the poll loop addresses it by
    # its instance name, and every op goes through the generic
    # instrument_message router.
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        api = Api(ResourceServer())
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        instruments = res["describe"]["instruments"]
        assert [i["name"] for i in instruments] == ["la"]
        (group,) = instruments
        assert group["grouped"] is True
        assert [m["name"] for m in group["members"]] == ["control.control",
                                                         "phy.control"]
        assert [m["sample_rate"] for m in group["members"]] == [100_000_000,
                                                                125_000_000]
        assert [m["kind"] for m in group["members"]] == ["raw", "raw"]
        assert group["composition"] == "raw"
        assert group["panel_url"].startswith("/r/")   # server-minted panel URL
        # The shared trigger is a section of the analyzer's panel, addressed by
        # the name its block carries in the descriptor.
        assert [t["name"] for t in group["triggers"]] == ["control.trigger"]

        status = await api.poll("la")
        assert status["health"] is True
        assert status["state"] in ("idle", "armed", "capturing")

        assert "error" not in await api.instrument_message(
            "la", {"op": "configure",
                   "triggers": {"control.trigger": {"value": 0xC,
                                                    "mask": 0xC}}})  # state=DONE

        params = {"seconds": SPAN, "pre_seconds": PRE_SPAN}
        armed = await api.instrument_message("la", {"op": "arm",
                                                    "params": params})
        assert "error" not in armed, armed
        # The panel is told what each member ended up capturing.
        for name, (count, _) in SPAN_COUNTS.items():
            assert f"{name}: {count} samples" in armed["summary"]
        for _ in range(200):
            status = await api.poll("la")
            if status["state"] == "idle":
                break
            await asyncio.sleep(0.02)
        assert status["state"] == "idle" and status["triggered"]

        read = await api.instrument_message("la", {"op": "read",
                                                   "params": params})
        assert "error" not in read, read
        assert read["kind"] == "group" and read["timescale"] == "1 ps"
        assert read["scopes"] == ["capture.control.control",
                                  "capture.phy.control"]
        assert read["trace_url"].startswith("/r/") and "?t=" in read["trace_url"]
        vcd = Vcd(api.resolve_instrument("la").resource("trace.vcd")[0].decode())
        assert {"capture.control.control", "capture.phy.control"} <= vcd.scopes
        assert "error" not in await api.disconnect()

    asyncio.run(run())
