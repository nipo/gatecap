"""Logic-analyzer instrument driver: one or several correlated capture domains.

The instrument's envelope holds every block of the analyzer as a child --
control, trace buffer and trigger per domain -- and its tail names the capture
controls one arm covers. The driver binds those references once enumeration
has produced the children, and presents the analyzer as one node with the
blocks beneath it.

It orchestrates the group rather than owning registers: it configures and arms
every member (the gateware ANDs their ready lines into the shared trigger's
enable, so no member can be missed and the arm order does not matter), it
composes their states into one group state, and it reads every member back into
a single absolute-time VCD where all domains are aligned on the trigger (see
:mod:`.compose`). Every member call goes through the member's own ``Control``
driver, so per-domain validation, back-dating and buffer decoding stay in one
place.

The group's capture window is a duration, not a sample count: its members run
at different rates, so the same number of samples would cover different spans
of real time on each of them. Each member converts that window into its own
parameters -- sample counts for a raw member, a post-trigger time cap and a
ring of buffer lines for a run-length-encoded one (see :mod:`.plan`).

An analyzer with a single domain is a group of one: the same orchestration
over one member, captured in that member's own samples and shown as that
member's own trace, since a lone domain has nothing to be correlated with.

The analyzer is the whole user-facing surface of its blocks. Its panel holds
the trigger editors of the trigger blocks it contains and the capture controls
of its domains, so the triggers and the capture are edited where they are
armed and read.
"""

import uuid
from importlib.resources import files

from acrobe_plugin.gatecap.enumerator import (MemoryMappedEnumerator,
                                             MemoryMappedInstrument)
from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor, GuiAdaptor

from .blocks.control import Control, RleControl
from .blocks.trigger import EdgeTrigger, Trigger
from .compose import ComposedTrace
from .fetch import FetchProgress
from .plan import GroupWindow
from .waveform import WaveformView

# Must match LOGIC_ANALYZER_UUID_C in the gateware (gatecap.descriptor).
LOGIC_ANALYZER_UUID = uuid.UUID("ce4e395e-1439-4ab7-9cee-cfb4f3257f3d")


@MemoryMappedEnumerator.instruments.register(LOGIC_ANALYZER_UUID)
class LogicAnalyzer(MemoryMappedInstrument):
    # An analyzer has no state of its own; it reports its members' states
    # composed with the control block's own encoding, so a frontend reads one
    # vocabulary.
    STATE_IDLE = Control.STATE_IDLE
    STATE_ARMED = Control.STATE_ARMED
    STATE_CAPTURING = Control.STATE_CAPTURING
    STATE_READING = Control.STATE_READING
    STATE_NAMES = Control.STATE_NAMES

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        # Envelope tail: [ [ member control child names ] ].
        if not self.tail or not isinstance(self.tail[0], list):
            raise ValueError(
                f"logic analyzer {self.name!r} does not name the capture "
                f"controls one arm covers")
        control_names = self.tail[0]
        if not control_names:
            raise ValueError(
                f"logic analyzer {self.name!r} references no control block")
        for control_name in control_names:
            if not isinstance(control_name, str):
                raise TypeError(
                    f"logic analyzer {self.name!r} references {control_name!r},"
                    f" which is not a child name")
        self.control_names = list(control_names)
        # The window of the last group arm and the per-member parameters it
        # resolved to, so a later read reconstructs the same capture without
        # the caller repeating any of it.
        self.armed_window = None
        self.armed_plan = None
        self.armed_overrides = None
        # Host-side progress of a group readback: the group sizes every
        # member's transfer before the first word moves, so the composed
        # fraction has one denominator for the whole fetch (see poll()).
        self.fetch = FetchProgress()
        self.__controls = None

    def siblings_resolve(self, siblings):
        """Bind the referenced child names to their driver nodes. Called by
        the enumerator once every child of the instrument exists."""
        controls = []
        for control_name in self.control_names:
            child = siblings.get(control_name)
            if child is None:
                raise LookupError(
                    f"logic analyzer {self.name!r} references "
                    f"{control_name!r}, which is not one of its children "
                    f"(children: {', '.join(sorted(siblings))})")
            if not isinstance(child, Control):
                raise TypeError(
                    f"logic analyzer {self.name!r} references "
                    f"{control_name!r}, a {type(child).__name__}, not a "
                    f"capture control")
            controls.append(child)
        self.__controls = controls

    @property
    def child_controls(self):
        """The referenced capture controls, in descriptor order."""
        assert self.__controls is not None, (
            f"logic analyzer {self.name!r} was never resolved against its "
            f"children")
        return list(self.__controls)

    def trigger_node_get(self):
        """The one trigger block the whole group fires on. A group whose
        members do not name the same trigger is not correlated -- it would arm
        several independent events -- so that is rejected here rather than
        producing a trace whose domains share a meaningless origin."""
        controls = self.child_controls
        names = {control.trigger for control in controls}
        if len(names) != 1:
            raise ValueError(
                f"logic analyzer {self.name!r} is not a correlated group: its "
                f"members reference {len(names)} triggers ("
                + ", ".join(f"{c.name} -> {c.trigger}" for c in controls) + ")")
        return controls[0].trigger_node_get()

    @property
    def trigger_nodes(self):
        """Every trigger block of this analyzer, in descriptor order. A
        correlated group fires on one of them, but a descriptor may hold
        several -- a domain whose signals are watched without being traced
        has a trigger and no capture at all -- and each is configured on its
        own."""
        return [child for child in self.children
                if isinstance(child, (Trigger, EdgeTrigger))]

    def trigger_by_name(self, name):
        for node in self.trigger_nodes:
            if node.name == name:
                return node
        raise KeyError(
            f"logic analyzer {self.name!r} holds no trigger {name!r} "
            f"(triggers: {', '.join(n.name for n in self.trigger_nodes)})")

    @property
    def member_kinds(self):
        """``{member name: "raw" | "rle"}`` -- what parameter vocabulary each
        member takes, which is what a frontend needs to know to ask for a
        window the whole group can honour."""
        return {control.name: ("rle" if isinstance(control, RleControl)
                               else "raw")
                for control in self.child_controls}

    def plan(self, *, seconds=None, pre_seconds=None, windows=None,
             pre_lines=None, overrides=None, base=None):
        """Resolve a group window (durations, plus the RLE ring depth in
        buffer lines) into one set of parameters per member -- a
        :class:`.plan.GroupPlan`. Fields left None come from ``base`` (the
        window a previous call resolved) or from the window defaults;
        ``overrides`` ({member name: params}) replaces the derivation for a
        member, and wins over it."""
        window = (base or GroupWindow()).merged(
            seconds=seconds, pre_seconds=pre_seconds, windows=windows,
            pre_lines=pre_lines)
        return window.resolve(self.child_controls, overrides)

    async def configure_and_arm(self, *, seconds=None, pre_seconds=None,
                                windows=None, pre_lines=None, trigger=None,
                                overrides=None):
        """Configure and arm the whole group over a window given in real time:
        ``seconds`` of capture, ``pre_seconds`` of it before the trigger,
        ``windows`` of it (raw members only), and ``pre_lines`` of pre-trigger
        ring for run-length-encoded members. Each member converts that with its
        own capture clock and validates the result against its own buffer and
        limits. ``trigger``, if given, is the ``(value, mask)`` compare written
        once to the shared trigger block -- otherwise the trigger keeps
        whatever its own pane or the caller programmed.

        Arming order carries no meaning: the trigger's enable is the AND of
        every member's ready line, so it physically cannot fire before the last
        member holds its pre-trigger context.

        Returns the resolved plan, whose notes report every member whose
        window differs from the one asked for."""
        node = self.trigger_node_get()      # also validates that it is shared
        plan = self.plan(seconds=seconds, pre_seconds=pre_seconds,
                         windows=windows, pre_lines=pre_lines,
                         overrides=overrides)
        if trigger is not None:
            value, mask = trigger
            await node.configure(value, mask)
        for control in self.child_controls:
            await control.configure_and_arm(**plan.params(control.name))
        self.armed_window = plan.window
        self.armed_plan = plan
        self.armed_overrides = overrides
        return plan

    async def abort(self):
        """Abort every member, returning the whole group to idle."""
        for control in self.child_controls:
            await control.abort()

    async def status(self):
        """``(state, triggered, windows)`` composed over the members: the group
        is capturing while any member is, armed while any member is (and none
        capturing), idle only once every member is; it is triggered once every
        member is, and has completed the windows its slowest member has."""
        return self.__compose([await c.status() for c in self.child_controls])

    def __compose(self, states):
        # The state encoding is ordered idle < armed < capturing < reading, so
        # the busiest member's state is the group's.
        state = max(s[0] for s in states)
        triggered = all(s[1] for s in states)
        windows = min((s[2] for s in states if len(s) > 2), default=0)
        return state, triggered, windows

    def __report(self, entries):
        """The group's live-progress string, from one ``(member name, state,
        member progress)`` per member: empty while every member sits in the
        same state with nothing of its own to report (the group is then fully
        described by its state), otherwise each member's state and progress --
        which is what tells the user who the group is waiting on."""
        if (len({state for _, state, _ in entries}) == 1
                and not any(report for _, _, report in entries)):
            return ""
        return " · ".join(
            f"{name} {self.STATE_NAMES.get(state, '?')}"
            + (f" ({report})" if report else "")
            for name, state, report in entries)

    async def progress(self):
        """A short live-progress string for any frontend."""
        entries = []
        for control in self.child_controls:
            status = await control.status()
            entries.append((control.name, status[0], await control.progress()))
        return self.__report(entries)

    async def poll(self):
        """One status poll of the whole group, in the shape a frontend expects
        from a capture block: {state (name), tone, triggered, fingerprint,
        progress, fetch}. Each member is polled through its own burst read; the
        fingerprint is the instance's, so any member reports the same one. A
        member whose trace is being fetched answers from host memory, so a
        readback in flight -- the group's, or one member's own -- costs no
        transport traffic here either."""
        polls = [await control.poll_raw() for control in self.child_controls]
        state, triggered, _ = self.__compose(
            [(p["state"], p["triggered"], 0) for p in polls])
        fetches = [p["fetch"] for p in polls if p["fetch"]]
        if fetches:
            # The words every member has moved, against the size this group
            # planned for the whole read; a member reading on its own (from its
            # own pane) has no group plan, so its own size stands in.
            snapshot = FetchProgress.merge(
                fetches, total=self.fetch.total if self.fetch.active else 0)
            return Control.reported(
                {"state": self.STATE_READING, "triggered": triggered,
                 "fingerprint": polls[0]["fingerprint"],
                 "progress": FetchProgress.report(snapshot),
                 "fetch": snapshot})
        entries = [(control.name, p["state"], p["progress"])
                   for control, p in zip(self.child_controls, polls)]
        return Control.reported(
            {"state": state, "triggered": triggered,
             "fingerprint": polls[0]["fingerprint"],
             "progress": self.__report(entries), "fetch": None})

    def plan_for_read(self, seconds, pre_seconds, windows, pre_lines, overrides):
        """The per-member parameters a read uses: those of the last group arm
        when the caller repeats nothing, otherwise the arm's window refined by
        what it does give."""
        if all(field is None for field in (seconds, pre_seconds, windows,
                                           pre_lines, overrides)):
            if self.armed_plan is None:
                raise ValueError(
                    f"logic analyzer {self.name!r} has not been armed, so a "
                    f"read "
                    f"has no window to reuse: give one (seconds/pre_seconds)")
            return self.armed_plan
        return self.plan(seconds=seconds, pre_seconds=pre_seconds,
                         windows=windows, pre_lines=pre_lines,
                         overrides=overrides if overrides is not None
                         else self.armed_overrides,
                         base=self.armed_window)

    async def read_trace(self, *, seconds=None, pre_seconds=None, windows=None,
                         pre_lines=None, overrides=None):
        """Read every member's buffer into one group result dict:
        ``{"kind": "group", "members": [{"name", "result"}]}``, each member's
        result being exactly what its own driver produces. Called with no
        parameters it reuses the window of the last group arm."""
        plan = self.plan_for_read(seconds, pre_seconds, windows, pre_lines,
                                  overrides)
        resolved = {member.name: member.params for member in plan}
        # Every member counts as fetching for as long as the group read runs --
        # from the reads that size it, not just while its own turn is on the
        # wire: no pane may poll its registers over a link the group read is
        # saturating. A member's own readback nests inside this, keeping its
        # own counters.
        self.fetch.begin()
        for control in self.child_controls:
            control.fetch.begin()
        try:
            words = 0
            for control in self.child_controls:
                words += await control.read_plan(**resolved[control.name])
            self.fetch.expect(words)
            members = []
            for control in self.child_controls:
                result = await control.read_trace(**resolved[control.name])
                members.append({"name": control.name, "result": result})
        finally:
            for control in self.child_controls:
                control.fetch.end()
            self.fetch.end()
        return {"kind": "group", "members": members}

    async def capture(self, value, mask, *, seconds=None, pre_seconds=None,
                      windows=None, pre_lines=None, overrides=None,
                      settle_tries=2000):
        """One-shot: program the shared trigger, group-arm over the given
        window, wait for every member to return to idle (or abort), then read
        the group back. The counterpart of ``Control.capture`` for a correlated
        group."""
        await self.configure_and_arm(trigger=(value, mask), seconds=seconds,
                                     pre_seconds=pre_seconds, windows=windows,
                                     pre_lines=pre_lines, overrides=overrides)
        triggered = False
        for _ in range(settle_tries):
            state, triggered, _ = await self.status()
            if state == self.STATE_IDLE:
                break
        else:
            await self.abort()
            triggered = False
        result = await self.read_trace()
        result["triggered"] = triggered
        return result

    def compose(self, result):
        """The group result as one absolute-time composition (see
        :class:`.compose.ComposedTrace`)."""
        return ComposedTrace.from_result(result)

    def grouped(self):
        """Whether the analyzer correlates several domains. A group of one is
        captured in its member's own samples and read back as its member's own
        trace; only a real group needs the window in real time and the
        composed trace that follows from it."""
        return len(self.child_controls) > 1

    def ui_adaptor(self, frontend, resources=None):
        cached = self.__dict__.get(f"ui_{frontend}")
        if cached is not None:
            return cached
        if frontend == "console":
            # A group of one says nothing its member's own info lines do not.
            if not self.grouped():
                return None
            adaptor = AnalyzerConsole(self)
        elif frontend == "gui":
            adaptor = AnalyzerGui(self, resources)
        else:
            return None
        self.__dict__[f"ui_{frontend}"] = adaptor
        return adaptor


class AnalyzerConsole(ConsoleAdaptor):
    """Console UI for the analyzer: names the correlated capture controls and
    what each of them samples, and renders a group capture."""

    def info(self):
        d = self.driver
        controls = d.child_controls
        lines = [f"{d.name}:",
                 f"  correlated capture group of {len(controls)} control(s)"]
        for control in controls:
            rate = (f"{control.sample_rate / 1e6:g} MHz" if control.sample_rate
                    else "unknown")
            lines.append(f"  member {control.name}: {control.signal_count} "
                         f"probes, sample clock {rate}, trigger integration "
                         f"latency {control.integration_latency} cycle(s)")
        return lines

    def render(self, result, fmt):
        """The group capture as bytes. Only VCD can carry it: its members run
        on different clocks, so their samples share no row and no index -- a
        table format would have to either drop domains or invent a common
        sample index that no member actually has. Anything else is refused
        rather than written wrong."""
        if fmt != "vcd":
            names = ", ".join(m["name"] for m in result["members"])
            raise ValueError(
                f"a correlated capture group ({names}) has one timebase per "
                f"member; {fmt!r} cannot express that -- render it as vcd")
        return self.driver.compose(result).to_vcd()[0]


class AnalyzerGui(GuiAdaptor, WaveformView):
    """Web UI for the analyzer: the one panel of the whole instrument. It
    holds the editors of the trigger blocks it contains, the capture window
    over its domains, the Arm/Read/Abort actions, and the trace on its own
    waveform surface -- a group of one shows its member's own trace, a real
    group the composed one, every domain in its own scope on one absolute time
    axis."""

    PANEL = files(__package__).joinpath("panel.js")
    ORDER = 20   # above the panels that own no waveform

    def __init__(self, driver, resources):
        super().__init__(driver, resources)
        self.vcd = b""
        self.serial = 0

    def describe(self):
        d = self.driver
        kinds = d.member_kinds
        meta = {"name": self.address(), "type": str(LOGIC_ANALYZER_UUID),
                # What the panel must offer: durations for the raw members, a
                # ring depth in lines for the RLE ones, both when they mix. A
                # group of one takes its member's own parameters instead.
                "composition": self.composition(set(kinds.values())),
                "grouped": d.grouped(),
                "members": [{"name": c.name, "kind": kinds[c.name],
                             "sample_rate": c.sample_rate,
                             "signal_count": c.signal_count,
                             "max_length": getattr(c, "max_length", None),
                             "max_windows": getattr(c, "max_windows", None),
                             "integration_latency": c.integration_latency}
                            for c in d.child_controls],
                "triggers": [node.describe() for node in d.trigger_nodes]}
        meta["key"] = self.panel_key(meta)
        return meta

    @staticmethod
    def composition(kinds):
        return kinds.pop() if len(kinds) == 1 else "mixed"

    def resource(self, name):
        if name == "trace.vcd":
            return self.vcd, "text/plain"
        return super().resource(name)

    # The capture fields a panel sends, in the driver's vocabulary: the group
    # window (the panel works in the display unit it chooses and sends
    # seconds), plus the per-member parameters a domain captured in its own
    # samples is given directly.
    WINDOW_FIELDS = ("seconds", "pre_seconds", "windows", "pre_lines",
                     "overrides")

    @classmethod
    def window(cls, params):
        unknown = set(params) - set(cls.WINDOW_FIELDS)
        if unknown:
            raise ValueError(
                f"a capture window has no field(s) "
                f"{', '.join(sorted(unknown))} (it takes "
                f"{', '.join(cls.WINDOW_FIELDS)})")
        return {key: value for key, value in params.items() if value is not None}

    async def message(self, msg):
        op = msg.get("op")
        driver, params = self.driver, dict(msg.get("params", {}))
        if op == "configure":
            return {"ok": True, "summary": await self.__triggers(msg)}
        if op == "arm":
            plan = await driver.configure_and_arm(**self.window(params))
            return {"ok": True, "summary": plan.summary()}
        if op == "abort":
            await driver.abort()
            return {"ok": True}
        if op == "read":
            return self.__present(await driver.read_trace(**self.window(params)))
        if op == "capture":
            value, mask = params.pop("value", 0), params.pop("mask", 0)
            return self.__present(
                await driver.capture(value, mask, **self.window(params)))
        raise ValueError(f"unknown op {op!r}")

    async def __triggers(self, msg):
        """Write the compares the panel's trigger editors hold. One message
        carries every trigger of the instrument, so the framework's replay of
        the last "configure" restores the whole analyzer after a reconnect,
        whatever the user edited last."""
        summaries = []
        for name, params in msg.get("triggers", {}).items():
            node = self.driver.trigger_by_name(name)
            summaries.append(f"{name}: {await node.apply(params)}")
        if not summaries:
            raise ValueError(
                f"configuring logic analyzer {self.driver.name!r} names no "
                f"trigger")
        return " · ".join(summaries)

    def __present(self, result):
        """The trace as the panel loads it. A correlated group is composed
        onto one absolute time axis; a group of one is its member's own trace,
        on its own sample grid -- there is nothing to align it with, and its
        member may not even report a capture clock."""
        members = result["members"]
        if len(members) > 1:
            composed = self.driver.compose(result)
            self.vcd, markers = composed.to_vcd()
            timescale = composed.timebase.timescale
            scopes = [".".join(m.scope) for m in composed.members]
        else:
            member = members[0]
            self.vcd, markers = self.to_vcd(member["result"])
            timescale = None
            scopes = ["capture"]
        self.serial += 1
        trace_url = self.resources.mint(self, "trace.vcd") + "?t=" + str(self.serial)
        return {"markers": markers, "serial": self.serial, "kind": "group",
                "timescale": timescale, "trace_url": trace_url,
                "scopes": scopes}
