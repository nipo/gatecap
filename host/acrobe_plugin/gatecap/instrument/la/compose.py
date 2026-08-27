"""Composition of several capture domains onto one absolute-time VCD.

Each member of a correlated capture group samples on its own clock, so its
buffer is a sequence of cycles, not of seconds. A shared trigger is what the
members have in common: every member's trigger instant is the same physical
event, so placing sample ``i`` of a member at ``(i - trigger) / f_member``
puts every domain on one real-time axis, whatever their rates.

VCD timestamps are non-negative integers on a single timescale, so the
composition picks a tick fine enough to place every domain's sample instants
exactly (see ``Timebase``) and shifts the whole picture so that the earliest
sample of the capture lands at tick 0. The trigger instant -- the common
origin the members are aligned on -- is reported as a marker rather than being
the file's zero, which a VCD cannot express with pre-trigger samples present.

Back-dating is not redone here: each member's result dict already places its
trigger where the capture path put it (the raw core back-dates its window in
hardware by the trigger's intrinsic latency plus its own integration latency;
an RLE member reports that same sum as ``trigger_latency`` for the host to
apply). ``DomainTrace`` reads that placement and nothing else.
"""

from __future__ import annotations

import io
import logging
from fractions import Fraction

from vcd import VCDWriter

from .signals import VcdLayout

LOG = logging.getLogger(__name__)


class Timebase:
    """The tick every member's sample instants are expressed in.

    A candidate resolution is usable when every member's sample period is a
    whole number of ticks: 1 ps covers the integer-MHz clocks a capture core
    runs on (10 ns at 100 MHz, 8 ns at 125 MHz), and a rate whose period is not
    a whole number of picoseconds pushes the tick down to 100, 10 or 1 fs. A
    period that is not whole even in femtoseconds is rounded, which no viewer
    can resolve, and logged."""

    RESOLUTIONS = ((1000, "1 ps"), (100, "100 fs"), (10, "10 fs"), (1, "1 fs"))
    FS_PER_SECOND = 10 ** 15

    def __init__(self, rates):
        self.rates = sorted({int(rate) for rate in rates})
        if not self.rates:
            raise ValueError("a timebase needs at least one sample rate")
        for rate in self.rates:
            if rate <= 0:
                raise ValueError(f"sample rate {rate} is not usable as a "
                                 f"timebase")
        periods = [Fraction(self.FS_PER_SECOND, rate) for rate in self.rates]
        for resolution, timescale in self.RESOLUTIONS:
            if all((period / resolution).denominator == 1 for period in periods):
                self.resolution_fs, self.timescale = resolution, timescale
                break
        else:
            self.resolution_fs, self.timescale = self.RESOLUTIONS[-1]
            LOG.warning("sample rates %s have no exact femtosecond period; "
                        "sample instants are rounded to 1 fs", self.rates)

    def period(self, rate):
        """Sample period of ``rate``, in ticks of this timebase."""
        ticks = Fraction(self.FS_PER_SECOND, int(rate)) / self.resolution_fs
        return int(ticks) if ticks.denominator == 1 else round(ticks)


class DomainTrace:
    """One member's contribution to a composed capture: its samples, and the
    cycle offset of each relative to that member's trigger instant."""

    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.kind = result["kind"]
        if self.kind not in ("raw", "rle"):
            raise ValueError(f"member {name!r} produced a {self.kind!r} trace, "
                             f"which cannot be placed in absolute time")
        self.sample_rate = result.get("sample_rate") or 0
        if not self.sample_rate:
            raise ValueError(
                f"member {name!r} reports no sample clock, so its samples "
                f"cannot be placed on a common time axis")
        self.layout = VcdLayout(result["names"], buses=True,
                                enums=result.get("enums"))

    @property
    def scope(self):
        """VCD scope path this member's variables hang under: the shared
        capture root, then the member's dotted block name (``phy.control`` ->
        ``capture.phy.control``), so domains never collide."""
        return ("capture",) + tuple(self.name.split("."))

    @property
    def window_count(self):
        return len(self.result["windows"]) if self.kind == "raw" else 1

    def events(self, window):
        """``(cycle, sample)`` pairs of one window, the cycle counted from
        this member's trigger instant (negative before it)."""
        if self.kind == "raw":
            samples = self.result["windows"][window]
            trigger = self.result.get("trigger_index") or 0
            return [(i - trigger, sample) for i, sample in enumerate(samples)]
        if window:
            raise IndexError(f"member {self.name!r} is run-length encoded and "
                             f"has a single window")
        runs = self.result["runs"]
        # The decoded stream starts at cycle 0; the trigger sits at the end of
        # the pre-trigger runs, skewed back into them by the latency the member
        # reports (see the module docstring).
        trigger = (sum(dwell for _, dwell in runs[:self.result["trigger_run"]])
                   - self.result.get("trigger_latency", 0))
        out = []
        cycle = 0
        for value, dwell in runs:
            out.append((cycle - trigger, value))
            cycle += dwell
        return out


class ComposedTrace:
    """The correlated capture of a whole group as one VCD.

    Members keep their own sample rates; a window is laid out around its
    trigger, which every member shares, and successive windows follow one
    another with a gap so their samples never interleave."""

    def __init__(self, members):
        self.members = [DomainTrace(name, result) for name, result in members]
        if not self.members:
            raise ValueError("a composed trace needs at least one member")
        counts = {m.window_count for m in self.members}
        if len(counts) != 1:
            raise ValueError(
                "members disagree on the number of captured windows: "
                + ", ".join(f"{m.name}={m.window_count}" for m in self.members))
        self.window_count = counts.pop()
        self.timebase = Timebase(m.sample_rate for m in self.members)
        self.periods = [self.timebase.period(m.sample_rate) for m in self.members]
        self.__place()

    @classmethod
    def from_result(cls, result):
        """Build from a ``LogicAnalyzer.read_trace`` result dict."""
        if result.get("kind") != "group":
            raise ValueError(f"not a group capture result: {result.get('kind')!r}")
        return cls([(m["name"], m["result"]) for m in result["members"]])

    def __place(self):
        """Resolve every event to an absolute tick, and the markers that
        annotate the file. Windows are laid out back to back, each one's
        trigger at its own origin."""
        # Per window, per member: (tick relative to the trigger, sample).
        relative = [[[(cycle * period, sample)
                      for cycle, sample in member.events(window)]
                     for member, period in zip(self.members, self.periods)]
                    for window in range(self.window_count)]
        gap = max(self.periods)
        self.events = []          # (tick, member index, sample), time ordered
        self.markers = []
        origin = 0
        for window, per_member in enumerate(relative):
            ticks = [t for member in per_member for t, _ in member]
            first, last = min(ticks), max(ticks)
            origin -= first       # the earliest sample of this window at origin+first
            for index, member in enumerate(per_member):
                for tick, sample in member:
                    self.events.append((origin + tick, index, sample))
            if self.window_count > 1:
                if window:
                    self.markers.append((f"w{window}", origin + first))
                self.markers.append((f"trig{window}", origin))
            else:
                self.markers.append(("trigger", origin))
            origin += last + gap
        self.events.sort(key=lambda event: (event[0], event[1]))

    @property
    def trigger_ticks(self):
        """Absolute tick of each window's trigger instant -- the common origin
        every member is aligned on."""
        return [tick for name, tick in self.markers if name.startswith("trig")]

    def to_vcd(self):
        """``(bytes, markers)``: the composed VCD and the ``(name, tick)``
        markers annotating it (the trigger, plus per-window boundaries when
        several windows were captured)."""
        buf = io.StringIO()
        with VCDWriter(buf, timescale=self.timebase.timescale, date="",
                       comment="gatecap correlated capture") as writer:
            for member in self.members:
                member.layout.register(writer, root=member.scope)
            for tick, index, sample in self.events:
                self.members[index].layout.emit(writer, tick, sample)
        return buf.getvalue().encode(), list(self.markers)
