"""The crossings between a bus explorer's shell and its core.

The shell runs on the host clock, the core on the clock of the target bus; the
contract between them (``gatecap.bus_explorer``) is two valid/ready streams,
one command going down and one response coming back. Each crosses through an
``interdomain_fifo_slice``, and when the two clocks are one -- an instance with
no declared clock, or one whose clock the rack itself runs on -- the crossing
is three plain assignments, which is what the same-clock case must cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from acrobe_plugin.gatecap.generator import Assignment, Cdc, Instance


@dataclass(frozen=True)
class Stream:
    """One end of a valid/ready stream, named by the signals carrying it."""

    data: str
    valid: str
    ready: str


class StreamCdc(Cdc):
    """The stream crossings of one explorer, in one direction."""

    FIFO = "nsl_clocking.interdomain.interdomain_fifo_slice"

    def deps(self):
        """A collapsed crossing instantiates nothing."""
        return () if self.direct() else (self.DEP,)

    def stream(self, label, width, source, destination, reset_n):
        """One stream carried from the source domain into the target one.

        The slice resynchronises the one reset it is given into both domains,
        and the reset it is given is the host's: the shell owns the protocol,
        and its clock is the one running whenever a transaction is in
        flight."""
        if self.direct():
            self.statements.append(Assignment(destination.data, source.data))
            self.statements.append(Assignment(destination.valid, source.valid))
            self.statements.append(Assignment(source.ready, destination.ready))
            return
        self.statements.append(Instance(
            f"{label}_cdc", self.FIFO,
            generic_map={"data_width_c": width},
            port_map={
                "reset_n_i": reset_n,
                "clock_i(0)": self.source.clock,
                "clock_i(1)": self.target.clock,
                "in_data_i": source.data,
                "in_valid_i": source.valid,
                "in_ready_o": source.ready,
                "out_data_o": destination.data,
                "out_valid_o": destination.valid,
                "out_ready_i": destination.ready,
                }))
