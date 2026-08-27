"""VCD authoring for the panes that show a captured trace.

``WaveformView`` is a mixin: a presentation adaptor mixes it in to render a
capture result to the bytes Surfer reads, and a console adaptor reuses it to
write the same bytes to a file, so the two frontends cannot disagree about
what a capture looks like.
"""

from __future__ import annotations

import io

from vcd import VCDWriter

from .signals import VcdLayout


class WaveformView:
    """Mixin: render a capture result dict to VCD bytes for Surfer, plus the
    markers that annotate it. A presentation adaptor mixes this in; a console
    adaptor can reuse it to write a file."""

    def to_vcd(self, result):
        """Render a result dict to VCD bytes. Buses are grouped (Surfer renders
        vector vars natively) and dotted names nest into scopes. Also returns a
        list of (name, time) markers: the trigger, and for a multi-window
        capture the per-window boundaries and triggers."""
        rate = result.get("sample_rate") or 0
        if rate:
            period_ps, timescale = max(1, round(1_000_000_000_000 / rate)), "1 ps"
        else:
            period_ps, timescale = 1, "1 ns"
        layout = VcdLayout(result["names"], buses=True, enums=result.get("enums"))
        buf = io.StringIO()
        with VCDWriter(buf, timescale=timescale, date="",
                       comment="gatecap capture") as writer:
            layout.register(writer)
            if result["kind"] == "raw":
                samples = [s for win in result["windows"] for s in win]
                for i, sample in enumerate(samples):
                    layout.emit(writer, i * period_ps, sample)
            else:
                t = 0
                for value, count in result["runs"]:
                    layout.emit(writer, t * period_ps, value)
                    t += count
        return buf.getvalue().encode(), self.__markers(result, period_ps)

    @staticmethod
    def __markers(result, period_ps):
        if result["kind"] == "rle":
            # Trigger sits at the pre/post boundary, skewed back into the
            # pre-region by the trigger's latency (see RleControl.read_trace).
            t = sum(c for _, c in result["runs"][:result["trigger_run"]])
            t = max(0, t - result.get("trigger_latency", 0))
            return [("trigger", t * period_ps)]
        windows = result["windows"]
        pre = result.get("trigger_index") or 0
        if len(windows) <= 1:
            return [("trigger", pre * period_ps)]
        # Windows are concatenated back to back; mark each window's start (a
        # boundary) and its trigger.
        count = len(windows[0])
        marks = []
        for w in range(len(windows)):
            base = w * count
            if w > 0:
                marks.append((f"w{w}", base * period_ps))          # boundary
            marks.append((f"trig{w}", (base + pre) * period_ps))   # trigger
        return marks
