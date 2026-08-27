"""Host-side progress of a trace readback.

Reading a captured trace back is a bulk transfer over the same link the status
polls use, and on a slow transport (JTAG: seconds for a few thousand words) it
is long enough that a frontend must show it. A capture block owns one
``FetchProgress``: its readback loop declares how many APB words the whole
transfer will move and advances the counter burst by burst, so a status poll
arriving while the trace is in flight answers from this object -- reporting the
transfer, not stale hardware state -- instead of adding traffic to a link the
fetch is already saturating.
"""


class FetchProgress:
    """Words moved against words planned, for one readback in flight.

    ``begin``/``end`` nest: a correlated group read marks every member fetching
    for the whole group transfer, and each member's own readback nests inside
    without resetting the counters.
    """

    def __init__(self):
        self.total = 0      # APB words the fetch in flight will transfer
        self.done = 0       # words transferred so far
        self.__depth = 0    # open begin/end pairs

    @property
    def active(self):
        return self.__depth > 0

    def begin(self):
        """Mark a fetch in flight, before its size is known -- the reads that
        size it are part of it. The outermost begin zeroes the counters."""
        if self.__depth == 0:
            self.total = 0
            self.done = 0
        self.__depth += 1

    def expect(self, words):
        """Declare words the fetch in flight will transfer. Called once the
        readback knows its plan, which is before any of it is on the wire."""
        self.total += words

    def end(self):
        assert self.__depth > 0, "fetch progress ended without a matching begin"
        self.__depth -= 1

    def advance(self, words):
        self.done += words

    @property
    def fraction(self):
        # A fetch whose size is not known yet reports 0 rather than a fraction
        # of nothing.
        if not self.total:
            return 0.0
        return min(1.0, self.done / self.total)

    def snapshot(self):
        """The poll payload of a fetch in flight: words done, words planned,
        and the fraction between them."""
        return {"done": self.done, "total": self.total,
                "fraction": self.fraction}

    @staticmethod
    def merge(parts, total=0):
        """One snapshot over several fetches (a group read over its members):
        their words done, against ``total`` when the caller planned the whole
        set before the first word moved, else against what the parts have
        planned so far."""
        done = sum(part["done"] for part in parts)
        total = total or sum(part["total"] for part in parts)
        return {"done": done, "total": total,
                "fraction": min(1.0, done / total) if total else 0.0}

    @staticmethod
    def report(snapshot):
        """The live-progress string a frontend shows for a fetch, in the style
        of the capture progress strings."""
        if not snapshot["total"]:
            return f"read {snapshot['done']} words"
        return (f"read {snapshot['done']}/{snapshot['total']} words · "
                f"{round(100 * snapshot['fraction'])}%")
