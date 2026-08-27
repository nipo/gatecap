"""Capture control drivers: the raw/multi-window ``Control`` and the
run-length-encoded ``RleControl``. Both drive a capture core that streams
into a trace buffer; each references its sink buffer and trigger block by
name and reaches the hardware through the bridge's register/memory ops.
"""

import csv
import io
import uuid

from acrobe_plugin.gatecap.enumerator import (MemoryMappedBlock,
                                             MemoryMappedEnumerator)
from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor

from ...fetch import FetchProgress
from .....names import SignalNames
from ...signals import VcdLayout
from ...waveform import WaveformView

# Must match the UUIDs in the gateware (gatecap.descriptor).
CONTROL_UUID = uuid.UUID("bf023668-f44d-46f0-a318-03aa06223021")
RLE_CONTROL_UUID = uuid.UUID("5d3f8a21-9e74-4c60-b1d2-6f0a83e5c497")


def fmt_time(seconds):
    """A short human-readable duration, shared by every frontend's progress."""
    if seconds >= 1:
        return f"{seconds:.2f} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.2f} ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:.1f} µs"
    return f"{seconds * 1e9:.0f} ns"


@MemoryMappedEnumerator.db.register(CONTROL_UUID)
class Control(MemoryMappedBlock):
    # Register map: one 0x100-stride group per role, matching the gatecap core
    # convention -- action (commands), config, status, arrays. A whole group is
    # contiguous so it can be read/written in one burst transaction.
    ACTION_BASE = 0x000
    CONFIG_BASE = 0x100
    STATUS_BASE = 0x200

    REG_COMMAND = ACTION_BASE + 0x00
    REG_CAPTURE_LEN = CONFIG_BASE + 0x00      # config group, contiguous
    REG_PRE_TRIGGER_LEN = CONFIG_BASE + 0x04
    REG_WINDOW_COUNT = CONFIG_BASE + 0x08
    REG_STATUS = STATUS_BASE + 0x00           # status group, contiguous
    REG_FINGERPRINT = STATUS_BASE + 0x04      # per-instance descriptor UID (RO)
    REG_HEAD_BASE = 0x300  # per-window head at REG_HEAD_BASE + window*4

    CMD_ARM = 1
    CMD_ABORT = 2

    STATE_IDLE = 0
    STATE_ARMED = 1
    STATE_CAPTURING = 2
    # Host-side state, not a hardware encoding (the status register's state
    # field only ever reports the three above): the capture is over and its
    # trace is being fetched over the transport, which on a slow link takes
    # seconds.
    STATE_READING = 3
    STATE_NAMES = {STATE_IDLE: "idle", STATE_ARMED: "armed",
                   STATE_CAPTURING: "capturing", STATE_READING: "reading"}
    # The tone a frontend colours each state with, from the vocabulary every
    # gatecap frontend styles (idle / active / attention / error). A capture
    # that has not fired yet is work in flight; one that has is what the user
    # is waiting to see, so it is raised to "attention" in reported().
    STATE_TONES = {STATE_IDLE: "idle", STATE_ARMED: "active",
                   STATE_CAPTURING: "active", STATE_READING: "active"}

    def __init__(self, bridge, base, name, obj):
        super().__init__(bridge, base, name)
        self.local_status_init()
        # [type, sink, trigger, signal-count, signal-names, max-length,
        #  max-windows, capture-clock-hz (0 = unknown), integration-latency].
        #  integration-latency is the trigger->core wiring/CDC delay (0 in the
        #  single-domain wire). sink and trigger are
        #  sibling names; signal-names is a grouping spec expanded to one name
        #  per probe bit.
        (_, self.sink, self.trigger, self.signal_count, names,
         self.max_length, self.max_windows, self.sample_rate,
         self.integration_latency) = obj
        self.signal_names, self.signal_enums = SignalNames.parse(names)
        self.window_count = 1   # last-armed config, for progress()

    def local_status_init(self):
        """Set up what a poll answers from host memory while a trace readback
        saturates the link: the readback's own progress, and the last hardware
        status read, which it stands in for. Both control flavours have it
        (RleControl builds itself, so it calls this too)."""
        self.fetch = FetchProgress()
        self.last_triggered = False
        self.last_fingerprint = None

    def pack(self, *values):
        """Little-endian byte string of `values` as consecutive words, for a
        burst write across a contiguous register group."""
        wb = self.bridge.word_bytes
        return b"".join(int(v).to_bytes(wb, "little") for v in values)

    async def configure(self, length, pre_trigger_len=0, window_count=1):
        # The trigger is a separate block (see Trigger.configure); this block
        # holds only length/pre-trigger/window config. CAPTURE_LEN,
        # PRE_TRIGGER_LEN and WINDOW_COUNT are contiguous -> one burst write.
        await self.bridge.mem_write(
            self.base + self.REG_CAPTURE_LEN,
            self.pack(length, pre_trigger_len, window_count))
        self.window_count = window_count

    async def head(self, window=0):
        # Start address of window `window` in the trace buffer, valid once
        # that window has completed.
        return await self.bridge.read32(
            self.base + self.REG_HEAD_BASE + window * 4)

    async def heads(self, count):
        return [await self.head(w) for w in range(count)]

    async def fingerprint(self):
        # Per-instance descriptor UID; same for every block, changes if the
        # gateware is reprogrammed with a different config.
        self.last_fingerprint = await self.bridge.read32(
            self.base + self.REG_FINGERPRINT)
        return self.last_fingerprint

    async def arm(self):
        await self.bridge.write32(self.base + self.REG_COMMAND, self.CMD_ARM)

    async def abort(self):
        await self.bridge.write32(self.base + self.REG_COMMAND,
                                  self.CMD_ABORT)

    async def status(self):
        # (state, triggered, windows-completed)
        s = await self.bridge.read32(self.base + self.REG_STATUS)
        return s & 0x3, bool(s & 0x4), (s >> 16) & 0xFFFF

    async def wait_done(self, tries=1000):
        for _ in range(tries):
            state, triggered, done = await self.status()
            if state == self.STATE_IDLE:
                return triggered, done
        raise TimeoutError("capture did not return to idle")

    async def progress(self):
        """A short live-progress string for any frontend, or "". Raw multi-
        window captures report windows completed against the armed count."""
        _, _, windows_done = await self.status()
        if self.window_count > 1:
            return f"windows {windows_done}/{self.window_count}"
        return ""

    def local_status_set(self, triggered, fingerprint):
        """Remember what a poll during a readback has to stand in for: the
        trigger flag and the fingerprint just read from the hardware (the state
        it reports is the readback's own)."""
        self.last_triggered = triggered
        self.last_fingerprint = fingerprint

    def fetch_poll(self):
        """The poll payload of a block whose trace is coming over the link: the
        host-side transfer progress, the hardware status last read, and no
        transport traffic at all -- the fetch's burst reads own the link, and a
        status read interleaved into them only makes the wait longer."""
        snapshot = self.fetch.snapshot()
        return {"state": self.STATE_READING, "triggered": self.last_triggered,
                "fingerprint": self.last_fingerprint,
                "progress": FetchProgress.report(snapshot), "fetch": snapshot}

    @classmethod
    def reported(cls, poll):
        """A raw poll as a frontend sees it: the state encoding replaced by the
        name the console and the CLI print, plus the tone a status pill is
        coloured by. No caller outside this package ever sees the encoding."""
        state, triggered = poll["state"], poll["triggered"]
        tone = ("attention" if triggered and state != cls.STATE_IDLE
                else cls.STATE_TONES[state])
        return dict(poll, state=cls.STATE_NAMES[state], tone=tone)

    async def poll(self):
        """This block's status for a frontend: {state (name), tone, triggered,
        fingerprint, progress, fetch}."""
        return self.reported(await self.poll_raw())

    async def poll_raw(self):
        """Everything a status poll needs, in one burst read of the status
        group: STATUS then FINGERPRINT (the two leading words at 0x200). The
        completed-window count (progress) is packed into STATUS. Returns a dict
        {state:int, triggered, fingerprint, progress, fetch}. While a trace
        readback is in flight it is answered from host memory instead (see
        fetch_poll)."""
        if self.fetch.active:
            return self.fetch_poll()
        wb = self.bridge.word_bytes
        data = await self.bridge.mem_read(self.base + self.REG_STATUS,
                                          2 * wb)   # 0x200..0x204
        s = int.from_bytes(data[0:wb], "little")               # STATUS      0x200
        fp = int.from_bytes(data[wb:2 * wb], "little")          # FINGERPRINT 0x204
        windows_done = (s >> 16) & 0xFFFF
        self.local_status_set(bool(s & 0x4), fp)
        return {"state": s & 0x3, "triggered": bool(s & 0x4), "fingerprint": fp,
                "progress": (f"windows {windows_done}/{self.window_count}"
                             if self.window_count > 1 else ""),
                "fetch": None}

    def sibling_node_get(self, name):
        """The enumerated block the descriptor refers to by ``name``. Sibling
        names are instance data -- a multi-domain core prefixes them with the
        domain (``rx.buffer``) -- so they are matched verbatim."""
        matches = self.parent.children_find(lambda x: x.name == name)
        if len(matches) != 1:
            raise LookupError(
                f"{self.name}: sibling reference {name!r} matches "
                f"{len(matches)} enumerated blocks")
        return matches[0]

    def sink_node_get(self):
        return self.sibling_node_get(self.sink)

    def trigger_node_get(self):
        return self.sibling_node_get(self.trigger)

    def trigger_latency(self):
        """Cycles of this core's clock between the matched condition and the
        trigger reaching the core: the trigger block's own pipeline (intrinsic,
        a constant of its type) plus the wiring/CDC delay the descriptor
        reports for this control (zero on a direct same-domain wire)."""
        return (getattr(self.trigger_node_get(), "LATENCY", 0)
                + self.integration_latency)

    # -- capture orchestration (frontend-free; the GUI/console adaptors and
    #    the one-shot convenience all funnel through these) ----------------

    def max_samples(self, windows=1):
        """The longest window this control can capture when ``windows``
        windows are armed: its own length limit, and the share of the trace
        buffer each window gets."""
        return min(self.max_length,
                   self.sink_node_get().depth // max(1, windows))

    def validate_capture(self, *, count=None, pretrigger=0, windows=1, **_):
        """Reject capture parameters the hardware cannot honour, with a
        message a frontend can show. Shared by the GUI and the CLI so both
        validate identically. Raises ValueError."""
        if count is None:
            raise ValueError("a sample count is required")
        if count < 1:
            raise ValueError("count must be at least 1")
        if count > self.max_length:
            raise ValueError(f"count {count} exceeds the max length "
                             f"{self.max_length}")
        if not 1 <= windows <= self.max_windows:
            raise ValueError(f"windows must be in [1, {self.max_windows}]")
        depth = self.sink_node_get().depth
        if count * windows > depth:
            raise ValueError(f"count x windows ({count} x {windows} = "
                             f"{count * windows}) exceeds the buffer depth "
                             f"{depth}")
        if not 0 <= pretrigger < count:
            raise ValueError(f"pretrigger must be in [0, count) = [0, {count})")

    async def configure_and_arm(self, *, count=None, pretrigger=0, windows=1,
                                pre_lines=0, max_seconds=0.0):
        """Validate, configure this control block and arm. The trigger is a
        separate block, configured by its own pane, so this no longer touches
        it. ``pre_lines`` and ``max_seconds`` are ignored here (see RleControl)."""
        self.validate_capture(count=count, pretrigger=pretrigger, windows=windows)
        await self.configure(count, pretrigger, windows)
        await self.arm()

    def _common(self):
        return {"names": self.signal_names, "signal_count": self.signal_count,
                "sample_rate": self.sample_rate, "enums": self.signal_enums}

    def window_plan(self, heads, count):
        """APB words the windows at ``heads`` will take to read back."""
        buf = self.sink_node_get()
        return sum(buf.window_words(h, count) for h in heads)

    async def read_plan(self, *, count=None, windows=1, **_):
        """The words a ``read_trace`` with these parameters will transfer, so a
        caller reading several blocks (a correlated group) can size the whole
        transfer before starting it."""
        return self.window_plan(await self.heads(windows), count)

    async def read_trace(self, *, count=None, windows=1, pretrigger=0):
        """Read back the captured windows as a result dict (see
        WaveformView.to_vcd). The trigger sits at ``pretrigger``: the core
        back-dates the window in hardware by trigger_latency(), so no host-side
        skew applies here."""
        buf = self.sink_node_get()
        # In flight from the first register read on, so a status poll never has
        # to reach the link the readback owns. Every window is then planned
        # before the first trace word moves, so the progress a poll reports
        # never revises its denominator mid-transfer.
        self.fetch.begin()
        try:
            heads = await self.heads(windows)
            self.fetch.expect(self.window_plan(heads, count))
            wins = [await buf.read_window(h, count, self.signal_count, self.fetch)
                    for h in heads]
        finally:
            self.fetch.end()
        return {**self._common(), "kind": "raw", "windows": wins,
                "trigger_index": pretrigger}

    async def _wait_idle(self, tries):
        for _ in range(tries):
            s = await self.status()
            if s[0] == self.STATE_IDLE:
                return s[1]
        await self.abort()
        return False

    async def capture(self, value, mask, *, count=None, pretrigger=0,
                      windows=1, pre_lines=0, max_seconds=0.0, settle_tries=2000):
        """One-shot: set the trigger, configure+arm, wait for idle (or the
        caller drives the DUT meanwhile), then read. Aborts if it never
        settles. Unlike the interactive path (trigger pane + configure_and_arm)
        this sets the trigger itself, so a script or self-test is self-contained.
        For a match-all trigger the core fills and idles on its own."""
        await self.trigger_node_get().configure(value, mask)
        await self.configure_and_arm(count=count, pretrigger=pretrigger,
                                     windows=windows, pre_lines=pre_lines,
                                     max_seconds=max_seconds)
        triggered = await self._wait_idle(settle_tries)
        result = await self.read_trace(count=count, windows=windows,
                                       pretrigger=pretrigger)
        result["triggered"] = triggered
        return result

    def ui_adaptor(self, frontend, resources=None):
        # The capture surface is the analyzer's panel, which arms and reads
        # this control as one of its domains; the block only describes itself.
        # Lazily built and cached; RleControl bypasses Control.__init__, so the
        # cache lives in the instance dict.
        if frontend != "console":
            return None
        cached = self.__dict__.get("ui_console")
        if cached is None:
            cached = self.__dict__["ui_console"] = ControlConsole(self)
        return cached


@MemoryMappedEnumerator.db.register(RLE_CONTROL_UUID)
class RleControl(Control):
    """Run-length-encoded capture control. Distinct type/driver: no window
    or length config; the whole encoded region is read from 0 and decoded.
    Shares the arm/trigger front end with Control by inheritance."""

    # COMMAND, STATUS and FINGERPRINT are inherited (same group offsets). RLE
    # adds config and status registers within the shared groups.
    REG_PRE_LINES = Control.CONFIG_BASE + 0x00
    REG_MAX_CYCLES = Control.CONFIG_BASE + 0x04
    REG_CYCLES = Control.STATUS_BASE + 0x08
    REG_END_PTR = Control.STATUS_BASE + 0x0C
    REG_PRE_HEAD = Control.STATUS_BASE + 0x10
    REG_PRE_N = Control.STATUS_BASE + 0x14

    def __init__(self, bridge, base, name, obj):
        MemoryMappedBlock.__init__(self, bridge, base, name)
        self.local_status_init()
        self.pre_lines = 0
        self.max_seconds = 0.0   # last-armed cap, for progress()
        # [type, sink, trigger, signal-count, signal-names, capture-clock-hz,
        #  integration-latency]
        (_, self.sink, self.trigger, self.signal_count, names,
         self.sample_rate, self.integration_latency) = obj
        self.signal_names, self.signal_enums = SignalNames.parse(names)

    async def configure(self, pre_lines=0, max_seconds=0.0):
        # The trigger is a separate block (see Trigger.configure). max_seconds
        # caps the post-trigger capture duration; it converts to real cycles
        # with the capture clock here, so every frontend works in seconds. 0
        # (or an unknown clock) means capture until the buffer fills.
        cycles = round(max_seconds * self.sample_rate) if self.sample_rate else 0
        # PRE_LINES and MAX_CYCLES are contiguous in the config group -> one
        # burst write.
        await self.bridge.mem_write(self.base + self.REG_PRE_LINES,
                                    self.pack(pre_lines, cycles))
        self.pre_lines = pre_lines
        self.max_seconds = max_seconds

    async def status(self):
        s = await self.bridge.read32(self.base + self.REG_STATUS)
        return s & 0x3, bool(s & 0x4)

    async def wait_done(self, tries=1000):
        for _ in range(tries):
            state, triggered = await self.status()
            if state == self.STATE_IDLE:
                return triggered
        raise TimeoutError("capture did not return to idle")

    def _decode(self, lines):
        # A tag-0 line is a sample; a tag-1 line repeats the last sample.
        # Leading counts with no reference sample (ring orphans) are dropped.
        tag_bit = 1 << self.signal_count
        mask = tag_bit - 1
        out = []
        last = None
        for line in lines:
            if line & tag_bit:
                if last is not None:
                    out += [last] * (line & mask)
            else:
                last = line & mask
                out.append(last)
        return out

    def _decode_runs(self, lines):
        # Like _decode but keeps (value, dwell) runs -- so a long idle stays
        # one entry instead of expanding to millions of samples.
        tag_bit = 1 << self.signal_count
        mask = tag_bit - 1
        runs = []
        for line in lines:
            if line & tag_bit:
                if runs:
                    v, c = runs[-1]
                    runs[-1] = (v, c + (line & mask))
            else:
                runs.append((line & mask, 1))
        return runs

    def region_plan(self, end):
        """APB words both encoded regions take to read back: the whole
        pre-trigger ring, then the post region up to the end pointer."""
        buffer = self.sink_node_get()
        return (buffer.transfer_words(0, self.pre_lines)
                + buffer.transfer_words(self.pre_lines, end - self.pre_lines))

    async def read_plan(self, **_):
        return self.region_plan(
            await self.bridge.read32(self.base + self.REG_END_PTR))

    async def _read_regions(self):
        buffer = self.sink_node_get()
        width = self.signal_count + 1
        # In flight from the first register read on (a poll must not reach the
        # link the readback owns), and both regions are planned from the end
        # pointer before either is read, so the whole readback has one
        # denominator.
        self.fetch.begin()
        try:
            end = await self.bridge.read32(self.base + self.REG_END_PTR)
            self.fetch.expect(self.region_plan(end))
            pre_seq = []
            if self.pre_lines:
                pre_head = await self.bridge.read32(self.base + self.REG_PRE_HEAD)
                pre_n = await self.bridge.read32(self.base + self.REG_PRE_N)
                ring = await buffer.read_contiguous(0, self.pre_lines, width,
                                                    self.fetch)
                pre_seq = [ring[(pre_head + i) % self.pre_lines]
                           for i in range(pre_n)]
            post = await buffer.read_contiguous(self.pre_lines,
                                                end - self.pre_lines, width,
                                                self.fetch)
        finally:
            self.fetch.end()
        return pre_seq, post

    async def read_capture(self):
        """Return (samples, trigger_index): the decoded stream with the
        trigger at trigger_index (pre-trigger samples precede it)."""
        pre_seq, post = await self._read_regions()
        pre = self._decode(pre_seq)
        return pre + self._decode(post), len(pre)

    async def read_runs(self):
        """Return (runs, trigger_run): (value, dwell) runs with the trigger
        at run index trigger_run. Keeps idle compressed for change output."""
        pre_seq, post = await self._read_regions()
        pre = self._decode_runs(pre_seq)
        return pre + self._decode_runs(post), len(pre)

    async def cycles(self):
        # Post-trigger real cycles so far (live capture progress toward the cap).
        return await self.bridge.read32(self.base + self.REG_CYCLES)

    async def fill(self):
        # Post-region lines used so far (live buffer fill).
        return await self.bridge.read32(self.base + self.REG_END_PTR)

    def __progress_str(self, cycles, fill):
        """Format the live progress string from the post-trigger cycle count
        and the post-region fill. Shared by progress() and poll()."""
        depth = self.sink_node_get().depth
        parts = []
        if self.sample_rate:
            elapsed = fmt_time(cycles / self.sample_rate)
            parts.append(f"{elapsed} / {fmt_time(self.max_seconds)}"
                         if self.max_seconds else elapsed)
        else:
            parts.append(f"{cycles} cyc")
        if depth:
            parts.append(f"buf {round(100 * fill / depth)}%")
        return " · ".join(parts)

    async def progress(self):
        """Live progress string: elapsed post-trigger time (toward the cap, if
        any) and buffer fill."""
        return self.__progress_str(await self.cycles(), await self.fill())

    async def poll_raw(self):
        """One 4-word burst of the status group -- STATUS, FINGERPRINT, CYCLES,
        END_PTR (the post-region fill) -- all contiguous, so the whole poll is
        a single transaction. Returns {state:int, triggered, fingerprint,
        progress, fetch}. While a trace readback is in flight it is answered
        from host memory instead (see fetch_poll)."""
        if self.fetch.active:
            return self.fetch_poll()
        wb = self.bridge.word_bytes
        data = await self.bridge.mem_read(self.base + self.REG_STATUS,
                                          4 * wb)   # 0x200..0x20c
        s = int.from_bytes(data[0:wb], "little")               # STATUS      0x200
        fp = int.from_bytes(data[wb:2 * wb], "little")          # FINGERPRINT 0x204
        cycles = int.from_bytes(data[2 * wb:3 * wb], "little")  # CYCLES      0x208
        fill = int.from_bytes(data[3 * wb:4 * wb], "little")    # END_PTR     0x20c
        self.local_status_set(bool(s & 0x4), fp)
        return {"state": s & 0x3, "triggered": bool(s & 0x4), "fingerprint": fp,
                "progress": self.__progress_str(cycles, fill), "fetch": None}

    def validate_capture(self, *, pre_lines=0, max_seconds=0.0, **_):
        depth = self.sink_node_get().depth
        if not 0 <= pre_lines < depth:
            raise ValueError(f"pre-lines must be in [0, {depth}) buffer lines")
        if max_seconds < 0:
            raise ValueError("max-time must be >= 0")
        if self.sample_rate and round(max_seconds * self.sample_rate) >= 2 ** 32:
            raise ValueError("max-time is too large for the 32-bit cycle cap")

    async def configure_and_arm(self, *, pre_lines=0, max_seconds=0.0, **_):
        self.validate_capture(pre_lines=pre_lines, max_seconds=max_seconds)
        await self.configure(pre_lines, max_seconds)
        await self.arm()

    async def read_trace(self, **_):
        runs, trig_run = await self.read_runs()
        # The trigger strobe (and so the written trigger sample) trails the
        # matched cycle by trigger_latency(); the pre-region holds those cycles,
        # so the marker is skewed back by it in software (the raw core
        # back-dates in hardware instead).
        return {**self._common(), "kind": "rle", "runs": runs,
                "trigger_run": trig_run,
                "trigger_latency": self.trigger_latency()}


class ControlConsole(ConsoleAdaptor, WaveformView):
    """Console UI for a capture control block: describes it, runs a one-shot
    capture, and renders the trace to CSV or VCD (VCD reuses WaveformView)."""

    def info(self):
        d = self.driver
        buf = d.sink_node_get()
        lines = [f"{d.name}:",
                 f"  probes ({d.signal_count}): {', '.join(d.signal_names)}"]
        if isinstance(d, RleControl):
            lines.append("  trigger: value-mask match, run-length encoded, "
                         "pre-trigger capable")
        else:
            lines.append(f"  trigger: value-mask match, up to {d.max_length} "
                         f"samples, up to {d.max_windows} window(s), "
                         f"pre-trigger capable")
        rate = d.sample_rate
        lines.append(f"  sample clock: {rate / 1e6:g} MHz" if rate
                     else "  sample clock: unknown")
        lines.append(f"  sink {buf.name}: {buf.sample_stride}-bit samples, "
                     f"depth {buf.depth} samples")
        return lines

    def render(self, result, fmt):
        """The captured trace as bytes in ``fmt`` ("vcd" or "csv")."""
        if fmt == "vcd":
            return self.to_vcd(result)[0]
        if result["kind"] == "rle":
            return self.__csv_rle(result).encode()
        return self.__csv(result).encode()

    @staticmethod
    def __layout(result):
        return VcdLayout(result["names"], enums=result.get("enums"))

    @staticmethod
    def __columns(layout):
        return [".".join(v.scope + (v.name,)) for v in layout.vars]

    @staticmethod
    def __cells(layout, sample):
        # One cell per bus/scalar: the decoded label for an enum field, else
        # the numeric value (scalars read 0/1).
        cells = []
        for var in layout.vars:
            value = var.value(sample)
            cells.append(var.label(value) or f"0x{value:x}" if var.enum else value)
        return cells

    @classmethod
    def __csv_rle(cls, result):
        # Change-based CSV: one row per run at its start cycle relative to the
        # trigger (pre-trigger runs are negative). Keeps a long/idle capture
        # compact instead of expanding runs to millions of samples.
        layout = cls.__layout(result)
        runs = result["runs"]
        # cycle 0 is the trigger, skewed back by the trigger latency.
        cycle = (-sum(dwell for _, dwell in runs[:result["trigger_run"]])
                 + result.get("trigger_latency", 0))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["cycle"] + cls.__columns(layout))
        for value, dwell in runs:
            writer.writerow([cycle] + cls.__cells(layout, value))
            cycle += dwell
        return out.getvalue()

    @classmethod
    def __csv(cls, result):
        # The trigger sample is at trigger_index; report indices relative to
        # it, so pre-trigger samples are negative.
        layout = cls.__layout(result)
        samples = [s for win in result["windows"] for s in win]
        pre = result.get("trigger_index") or 0
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["sample"] + cls.__columns(layout))
        for i, sample in enumerate(samples):
            writer.writerow([i - pre] + cls.__cells(layout, sample))
        return out.getvalue()
