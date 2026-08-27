"""Elaboration-time checks carried by a generated core.

A condition that depends on a stream configuration generic is not knowable
when the description is read, so it travels into the generated code as a
check on the elaborated value. Each one is emitted twice: a plain assert, which
covers simulation, and nsl_synthesis.assertion.synth_assert, which fails
elaboration under synthesis too (synthesisers are free to ignore a plain
assert).
"""

from __future__ import annotations

from dataclasses import dataclass

from .vhdl import Expr, Instance, RawStatement


@dataclass(frozen=True)
class Check:
    """One condition, its message and the label of its synthesis twin."""

    label: str
    message: str
    condition: str

    UNIT = "nsl_synthesis.assertion.synth_assert"
    DEP = "nsl_synthesis.assertion"

    def statements(self):
        return (RawStatement(f"assert {self.condition}\n"
                             f"  report {Expr.string(self.message)}\n"
                             "  severity failure;"),
                Instance(self.label, self.UNIT,
                         generic_map={"message_c": Expr.string(self.message),
                                      "condition_c": self.condition},
                         port_map={"unused_i": "'0'"}))
