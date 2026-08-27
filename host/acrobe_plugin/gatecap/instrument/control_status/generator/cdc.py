"""The crossings between a panel's register shell and its event core.

The shell runs on the host clock, the core on the instrument's own; the
contract between them (``gatecap.control_status``) is a set of whole 32-bit
words plus two strobes, and each of them crosses through the primitive its
meaning calls for. When the two clocks are one -- a panel with no declared
clock, or one whose clock the rack itself runs on -- every crossing is a plain
assignment of the whole array, which is what the framework crossing already
does for a scalar.

Counters are the one crossing that is not a plain resynchronisation: they are
gray-coded by ``interdomain_counter``, which needs its input to step by at most
one per cycle. Only the meaningful low bits may cross -- they wrap, and a wrap
is a single gray step at that width alone -- so the crossed value is
zero-extended back into a word on the far side.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (Assignment, Cdc, Instance,
                                             RawStatement, SignalDecl)


class PanelCdc(Cdc):
    """The shell-to-core crossings of one panel, in one direction."""

    COUNTER = "nsl_clocking.interdomain.interdomain_counter"
    WORD_WIDTH = "32"
    COUNTER_TYPE = "counter_vector"
    CROSSED_COUNTER = "crossed_counter_s"

    def deps(self):
        """A collapsed crossing instantiates nothing, whatever it carries."""
        return () if self.direct() else (self.DEP,)

    def words(self, label, origin, destination, count):
        """One resynchronised register per word: a level the destination
        domain reads at any time, never torn."""
        self.__cross(
            label, origin, destination, count, self.REG,
            lambda index: {"clock_i": self.target.clock,
                           "data_i": f"{origin}({index})",
                           "data_o": f"{destination}({index})"})

    def static_words(self, label, origin, destination, count):
        """One static register per word: a mask written far from any use, its
        settling covered by the strobe that follows it."""
        self.__cross(
            label, origin, destination, count, self.STATIC_REG,
            lambda index: {"input_clock_i": self.source.clock,
                           "data_i": f"{origin}({index})",
                           "data_o": f"{destination}({index})"})

    def __cross(self, label, origin, destination, count, unit, port_map):
        if self.direct():
            self.statements.append(Assignment(destination, origin))
            return
        for index in range(count):
            self.statements.append(Instance(
                f"{label}_{index}_cdc", unit,
                generic_map={"data_width_c": self.WORD_WIDTH},
                port_map=port_map(index)))

    def counters(self, label, origin, destination, count, width):
        """One gray-coded counter crossing per tick input, over the counter's
        own width."""
        if self.direct():
            self.statements.append(Assignment(destination, origin))
            return
        self.declarations.append(RawStatement(self.COUNTER_TYPE_DECLARATION))
        self.declarations.append(SignalDecl(
            self.CROSSED_COUNTER, f"{self.COUNTER_TYPE}({origin}'range)"))
        for index in range(count):
            self.statements.append(Instance(
                f"{label}_{index}_cdc", self.COUNTER,
                generic_map={"data_width_c": width},
                port_map={
                    "clock_in_i": self.source.clock,
                    "clock_out_i": self.target.clock,
                    "data_i": f"unsigned({origin}({index})"
                              f"({width}-1 downto 0))",
                    "data_o": f"{self.CROSSED_COUNTER}({index})",
                    }))
            self.statements.append(Assignment(
                f"{destination}({index})",
                f"std_ulogic_vector(resize("
                f"{self.CROSSED_COUNTER}({index}), 32))"))

    COUNTER_TYPE_DECLARATION = """\
-- Counters cross over their own width, and ride a full word on either side.
subtype counter_t is unsigned(counter_width_c-1 downto 0);
type counter_vector is array (natural range <>) of counter_t;"""
