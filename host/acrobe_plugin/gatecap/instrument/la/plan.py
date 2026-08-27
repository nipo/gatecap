"""Per-member capture parameters of a correlated group, derived from one
window expressed in real time.

Members of a group sample on different clocks, so an equal number of samples
covers unequal spans of real time: 1024 samples are 10.24 µs at 100 MHz and
5.12 µs at 200 MHz. A group window is therefore a duration, and each member
converts it with the capture clock its descriptor reports -- a raw member into
a sample count and a pre-trigger count, an RLE member into its post-trigger
time cap. An RLE member's pre-trigger ring stays expressed in buffer lines:
how much time a line covers is what the captured data says, so the span of a
ring cannot be known before the capture runs.

A member whose descriptor carries no capture clock cannot convert anything;
its parameters have to be given as raw sample counts through the per-member
overrides, which win over the derivation for every member.
"""

from .blocks.control import RleControl, fmt_time


class Duration:
    """A time value as the CLI accepts it: a number of seconds, optionally
    with a unit suffix (``12.5us``, ``2ms``, ``1s``, ``800ns``)."""

    # Per unit, what a value in it is divided by to reach seconds. Dividing by
    # the power of ten (rather than multiplying by its inverse) keeps round
    # values round: 10 µs is 1e-5 exactly, not 9.999999999999999e-06.
    UNITS = {"s": 1.0, "ms": 1e3, "us": 1e6, "µs": 1e6, "ns": 1e9}

    @classmethod
    def parse(cls, text):
        """Seconds as a float. Raises ValueError on anything else."""
        if text is None:
            return None
        text = str(text).strip()
        for suffix in sorted(cls.UNITS, key=len, reverse=True):
            if text.endswith(suffix):
                text, divisor = text[:-len(suffix)].strip(), cls.UNITS[suffix]
                break
        else:
            divisor = 1.0
        try:
            return float(text) / divisor
        except ValueError:
            raise ValueError(
                f"{text!r} is not a duration: give seconds, or a value with a "
                f"unit ({', '.join(sorted(cls.UNITS))})") from None


class MemberPlan:
    """One member's share of a group window: the parameters its own driver
    takes, the real time they cover, and the notes explaining any difference
    from what the group asked for (a span clamped to the member's buffer, or
    an override standing in for the derivation)."""

    def __init__(self, name, kind, params, pre_seconds=None, post_seconds=0.0,
                 pre_lines=None, notes=()):
        self.name = name
        self.kind = kind                    # "raw" | "rle"
        self.params = params
        # Real time this member actually covers around the trigger.
        # ``pre_seconds`` is None when the member keeps a ring of lines whose
        # span the capture decides (RLE), and ``pre_lines`` says how deep it is.
        self.pre_seconds = pre_seconds
        self.post_seconds = post_seconds
        self.pre_lines = pre_lines
        self.notes = list(notes)

    def summary(self):
        if self.kind == "rle":
            cap = (f"post-trigger cap {fmt_time(self.post_seconds)}"
                   if self.post_seconds else "post-trigger until the buffer fills")
            return (f"{self.name}: rle, pre-trigger ring {self.pre_lines} line(s), "
                    f"{cap}")
        count, pre = self.params["count"], self.params["pretrigger"]
        windows = self.params.get("windows", 1)
        if self.pre_seconds is None:
            # No capture clock: the counts came from an override and nothing
            # places them in time (such a member cannot be composed either).
            return (f"{self.name}: {count} samples, {pre} pre-trigger, "
                    f"{windows} window(s), no capture clock")
        return (f"{self.name}: {count} samples "
                f"({fmt_time(self.pre_seconds + self.post_seconds)}), "
                f"{pre} pre-trigger ({fmt_time(self.pre_seconds)}), "
                f"{windows} window(s)")


class GroupPlan:
    """What a whole correlated group was asked to capture: one MemberPlan per
    member, in descriptor order, plus the window they were derived from."""

    def __init__(self, window, members):
        self.window = window
        self.members = list(members)

    def __iter__(self):
        return iter(self.members)

    def params(self, name):
        for member in self.members:
            if member.name == name:
                return member.params
        raise KeyError(f"no member {name!r} in this group plan")

    @property
    def notes(self):
        return [note for member in self.members for note in member.notes]

    def lines(self):
        return ([member.summary() for member in self.members]
                + [f"note: {note}" for note in self.notes])

    def summary(self):
        return " · ".join(self.lines())


class GroupWindow:
    """A correlated capture window in real time: its total span, how much of
    it precedes the trigger, how many windows, and how deep an RLE member's
    pre-trigger ring is (in buffer lines -- see the module docstring).

    ``resolve`` turns it into one MemberPlan per member, each in that member's
    own parameter vocabulary."""

    def __init__(self, seconds=None, pre_seconds=0.0, windows=1, pre_lines=0):
        if seconds is not None and seconds <= 0:
            raise ValueError(f"a capture span must be positive, not {seconds}")
        if pre_seconds < 0:
            raise ValueError(
                f"a pre-trigger span must not be negative, not {pre_seconds}")
        if seconds is not None and pre_seconds >= seconds:
            raise ValueError(
                f"the pre-trigger span ({fmt_time(pre_seconds)}) must be shorter "
                f"than the whole window ({fmt_time(seconds)})")
        if windows < 1:
            raise ValueError(f"windows must be at least 1, not {windows}")
        if pre_lines < 0:
            raise ValueError(f"pre-lines must not be negative, not {pre_lines}")
        self.seconds = seconds
        self.pre_seconds = pre_seconds
        self.windows = windows
        self.pre_lines = pre_lines

    def merged(self, **fields):
        """A copy with the fields that are not None replaced -- so a read can
        refine the window an arm resolved without repeating all of it."""
        current = {"seconds": self.seconds, "pre_seconds": self.pre_seconds,
                   "windows": self.windows, "pre_lines": self.pre_lines}
        unknown = set(fields) - set(current)
        if unknown:
            raise TypeError(f"a capture window has no field(s) "
                            f"{', '.join(sorted(unknown))} (it takes "
                            f"{', '.join(sorted(current))})")
        current.update({k: v for k, v in fields.items() if v is not None})
        return GroupWindow(**current)

    @property
    def post_seconds(self):
        """The part of the window that follows the trigger. 0 when no span was
        asked for, which an RLE member reads as "until the buffer fills"."""
        return 0.0 if self.seconds is None else self.seconds - self.pre_seconds

    def resolve(self, controls, overrides=None):
        """One MemberPlan per control, with ``overrides`` ({member name:
        params}) replacing the derived parameters of that member."""
        overrides = dict(overrides or {})
        names = [control.name for control in controls]
        unknown = set(overrides) - set(names)
        if unknown:
            raise ValueError(
                f"no group member(s) {', '.join(sorted(unknown))} "
                f"(members: {', '.join(names)})")
        if self.windows > 1 and any(isinstance(c, RleControl) for c in controls):
            raise ValueError(
                "a group with a run-length-encoded member captures a single "
                "window; asked for " + str(self.windows))
        return GroupPlan(self, [
            (self.__rle(control, overrides.get(control.name, {}))
             if isinstance(control, RleControl)
             else self.__raw(control, overrides.get(control.name, {})))
            for control in controls])

    def __raw(self, control, override):
        """A raw member: the window converted to a sample count and a
        pre-trigger count at that member's rate, clamped to what its core and
        its share of the trace buffer hold."""
        rate, name = control.sample_rate, control.name
        params = {"windows": self.windows}
        notes = []
        if "count" in override:
            params.update({"pretrigger": 0, **override})
        elif not rate:
            raise ValueError(
                f"member {name!r} reports no capture clock, so a "
                f"{fmt_time(self.seconds or 0)} window cannot be converted to "
                f"samples; give it a count through the per-member overrides")
        elif self.seconds is None:
            raise ValueError(
                f"member {name!r} is a raw capture: it needs a capture span "
                f"(or a count through the per-member overrides)")
        else:
            limit = control.max_samples(self.windows)
            count = round(self.seconds * rate)
            if count < 1:
                raise ValueError(
                    f"member {name!r}: {fmt_time(self.seconds)} is shorter than "
                    f"one sample at {rate / 1e6:g} MHz")
            count = min(count, limit)
            params.update({"count": count,
                           "pretrigger": min(round(self.pre_seconds * rate),
                                             count - 1)})
            params.update(override)
        # Every difference from the requested span is reported, whether it
        # comes from the clamp above or from an override: the window a member
        # actually captures is never silently something else.
        if rate and self.seconds is not None:
            notes += self.__divergence(name, rate, params)
        pre = params["pretrigger"] / rate if rate else None
        post = (params["count"] - params["pretrigger"]) / rate if rate else 0.0
        return MemberPlan(name, "raw", params, pre_seconds=pre,
                          post_seconds=post, notes=notes)

    def __divergence(self, name, rate, params):
        asked_count = round(self.seconds * rate)
        asked_pre = round(self.pre_seconds * rate)
        notes = []
        if params["count"] != asked_count:
            notes.append(
                f"{name}: {fmt_time(self.seconds)} is {asked_count} samples at "
                f"{rate / 1e6:g} MHz, capturing {params['count']} "
                f"({fmt_time(params['count'] / rate)})")
        if params["pretrigger"] != asked_pre:
            notes.append(
                f"{name}: pre-trigger {fmt_time(self.pre_seconds)} is "
                f"{asked_pre} samples, keeping {params['pretrigger']} "
                f"({fmt_time(params['pretrigger'] / rate)})")
        return notes

    def __rle(self, control, override):
        """An RLE member: the post-trigger part of the window is its time cap,
        and the pre-trigger ring is the group's line count clamped to the
        buffer (its span is data-dependent, so it is not a duration)."""
        name = control.name
        cap = self.post_seconds
        if cap and not control.sample_rate:
            raise ValueError(
                f"member {name!r} reports no capture clock, so a "
                f"{fmt_time(cap)} post-trigger cap cannot be programmed; give "
                f"it a max_seconds of 0 (fill the buffer) through the "
                f"per-member overrides")
        depth = control.sink_node_get().depth
        lines, notes = self.pre_lines, []
        if lines >= depth:
            lines = depth - 1
            notes.append(f"{name}: pre-trigger ring {self.pre_lines} lines "
                         f"exceeds the {depth}-line buffer, keeping {lines}")
        params = {"pre_lines": lines, "max_seconds": cap, **override}
        return MemberPlan(name, "rle", params, pre_seconds=None,
                          post_seconds=params["max_seconds"],
                          pre_lines=params["pre_lines"], notes=notes)
