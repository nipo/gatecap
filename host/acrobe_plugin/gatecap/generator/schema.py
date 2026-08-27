"""Intermediate representation of a rack description.

The parser turns YAML into these dataclasses; everything downstream (assembly,
emission) reads them and never the raw YAML. They are frozen: a validated
description is immutable.

A description names a rack, a transport and the instruments the rack holds.
Everything below an instrument -- what it probes, how it is dimensioned -- is
its plugin's business and lives in the plugin's own intermediate
representation, reached through :attr:`Instrument.params`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DescriptionError
from .vhdl import Identifier


@dataclass(frozen=True)
class Name:
    """The dotted ``package.entity`` name of the generated rack."""

    package: str
    entity: str

    @classmethod
    def parse(cls, text, path):
        if not isinstance(text, str):
            raise DescriptionError("must be a dotted package.entity string", path)
        parts = text.split(".")
        if len(parts) != 2:
            raise DescriptionError(
                f"must be a dotted package.entity pair, got {text!r}", path)
        for part in parts:
            reason = Identifier.rejection(part)
            if reason:
                raise DescriptionError(f"name part {part!r} {reason}", path)
        if parts[0] == parts[1]:
            raise DescriptionError(
                "package and entity must differ (VHDL forbids reusing the "
                "name inside the package)", path)
        return cls(package=parts[0], entity=parts[1])

    def dotted(self):
        return f"{self.package}.{self.entity}"


@dataclass(frozen=True)
class Instrument:
    """One described instrument instance: its name, its type plugin and the
    keys the plugin validated.

    The name is the instance's whole identity: it is the name the descriptor
    envelope carries, the key of the instance's APB segment, and the prefix
    every boundary port, generic and constant of the instance takes."""

    name: str
    tag: str
    plugin: type
    params: dict

    def path(self):
        return f"instruments.{self.name}"

    def port(self, suffix):
        return f"{self.name}_{suffix}"

    def label(self, suffix):
        return f"{self.name}_{suffix}"

    def signal(self, suffix):
        return f"{self.name}_{suffix}_s"

    def constant(self, suffix):
        return f"{self.name}_{suffix}_c"


@dataclass(frozen=True)
class Communication:
    """The host transport and, when it is clocked, where its clock comes
    from: a clock an instrument exports, or ports of the rack's own.

    Whatever keys the mode adds of its own are its plugin's business and reach
    it back as ``params``."""

    mode: str
    plugin: type
    clock_export: str | None = None
    params: dict = field(default_factory=dict)

    def dedicated_clock(self):
        return self.clock_export is None


@dataclass(frozen=True)
class Description:
    """A validated description: everything decidable from YAML alone holds."""

    name: Name
    communication: Communication
    instruments: tuple

    def instrument(self, name):
        for instrument in self.instruments:
            if instrument.name == name:
                return instrument
        raise KeyError(name)

    def exported_clocks(self):
        """``<instance>.<clock>`` -> the instrument boundary port carrying
        it."""
        exports = {}
        for instrument in self.instruments:
            for clock, port in instrument.plugin.clocks(instrument).items():
                exports[f"{instrument.name}.{clock}"] = port
        return exports

    def exported_clock_rates(self):
        """``<instance>.<clock>`` -> its rate in Hz, for the clocks whose rate
        the description states."""
        rates = {}
        for instrument in self.instruments:
            for clock, hz in instrument.plugin.clock_rates(instrument).items():
                rates[f"{instrument.name}.{clock}"] = hz
        return rates

    def deps(self):
        """gbs dependency keys contributed by the instruments, first-seen
        order preserved. The transport's are the assembly's to add."""
        keys = []
        for instrument in self.instruments:
            for dep in instrument.plugin.deps(instrument):
                if dep not in keys:
                    keys.append(dep)
        return tuple(keys)
