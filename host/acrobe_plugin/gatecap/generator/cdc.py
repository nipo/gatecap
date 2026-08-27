"""Clock-domain crossings of a generated core.

Every crossing knows both of its clock domains. When the two share a clock the
crossing is an alias: the destination reads the source signal itself, nothing
is declared and nothing is instantiated. When they differ the matching NSL
interdomain primitive is instantiated -- a tick for an event, a static register
for set-and-hold configuration, a resynchronised register for a status level.

A crossing returns the expression the destination port must be associated
with, so a caller wires ports without knowing whether a primitive sits in
between.

Any instrument straddling the host clock and a clock of its own crosses the
same way, so this is framework machinery rather than one instrument's.
"""

from __future__ import annotations

from dataclasses import dataclass

from .vhdl import Instance, SignalDecl


@dataclass(frozen=True)
class ClockDomain:
    """A clock and its reset, named by the ports that carry them."""

    name: str
    clock: str
    reset_n: str


class Cdc:
    """Crossings from one clock domain into another.

    Declarations and statements accumulate in order of creation; the caller
    splices them into the architecture where they read best.
    """

    TICK = "nsl_clocking.interdomain.interdomain_tick"
    REG = "nsl_clocking.interdomain.interdomain_reg"
    STATIC_REG = "nsl_clocking.interdomain.interdomain_static_reg"
    DEP = "nsl_clocking.interdomain"

    # Destination-clock cycles from tick_i to tick_o through interdomain_tick:
    # the two resynchroniser stages its input toggle goes through, plus the
    # output edge-detect register that turns the toggle back into a pulse.
    TICK_LATENCY = 3

    # Samples a status level must hold steady before it is forwarded, for a
    # word whose bits must agree: a mid-transition sample then never reads a
    # value that never existed.
    STATE_STABLE_COUNT = 2

    def __init__(self, source, target):
        self.source = source
        self.target = target
        self.declarations = []
        self.statements = []

    def direct(self):
        return self.source.clock == self.target.clock

    def deps(self):
        return (self.DEP,) if self.statements else ()

    def tick(self, name, source):
        """A one-cycle event, retimed into the destination domain."""
        if self.direct():
            return source
        self.declarations.append(SignalDecl(name, "std_ulogic"))
        self.statements.append(Instance(
            self.__label(name), self.TICK,
            port_map={
                "input_clock_i": self.source.clock,
                "output_clock_i": self.target.clock,
                "input_reset_n_i": self.source.reset_n,
                "tick_i": source,
                "tick_o": name,
                }))
        return name

    def flag(self, name, source):
        """A one-bit level, resynchronised in the destination domain."""
        if self.direct():
            return source
        self.declarations.append(SignalDecl(name, "std_ulogic"))
        self.statements.append(Instance(
            self.__label(name), self.REG,
            generic_map={"data_width_c": "1"},
            port_map={
                "clock_i": self.target.clock,
                "data_i(0)": source,
                "data_o(0)": name,
                }))
        return name

    def level(self, name, source, width, stable=0, numeric=False):
        """A multi-bit status word, resynchronised in the destination domain.

        ``stable`` filters the value through that many identical samples,
        which a word whose bits must agree needs and a set of independent
        flags does not."""
        if self.direct():
            return source
        self.declarations.append(self.__vector(name, width))
        generic_map = {"data_width_c": width}
        if stable:
            generic_map["stable_count_c"] = str(stable)
        self.statements.append(Instance(
            self.__label(name), self.REG,
            generic_map=generic_map,
            port_map={
                "clock_i": self.target.clock,
                "data_i": self.__vector_of(source, numeric),
                "data_o": name,
                }))
        return self.__numeric_of(name, numeric)

    def static(self, name, source, width, numeric=False):
        """Set-and-hold configuration: written far from any use, so the
        destination samples it without a handshake."""
        if self.direct():
            return source
        self.declarations.append(self.__vector(name, width))
        self.statements.append(Instance(
            self.__label(name), self.STATIC_REG,
            generic_map={"data_width_c": width},
            port_map={
                "input_clock_i": self.source.clock,
                "data_i": self.__vector_of(source, numeric),
                "data_o": name,
                }))
        return self.__numeric_of(name, numeric)

    @staticmethod
    def __vector(name, width):
        top = str(int(width) - 1) if width.isdigit() else f"{width}-1"
        return SignalDecl(name, f"std_ulogic_vector({top} downto 0)")

    @staticmethod
    def __vector_of(source, numeric):
        return f"std_ulogic_vector({source})" if numeric else source

    @staticmethod
    def __numeric_of(name, numeric):
        return f"unsigned({name})" if numeric else name

    @staticmethod
    def __label(name):
        """Instance label of the crossing feeding ``name``."""
        stem = name[:-2] if name.endswith("_s") else name
        return f"{stem}_cdc"
