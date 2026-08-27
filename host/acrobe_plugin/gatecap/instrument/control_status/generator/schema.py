"""Intermediate representation of a ``!control-status`` instrument body.

The plugin's parser turns the tagged mapping into these frozen dataclasses;
emission reads them and never the raw YAML.

The four signal kinds are two shapes: a level (a control or a status) is one
named register of a declared width, optionally carrying an enumeration table;
a tick is one bit of a packed word, and a word is the simultaneity group the
description drew.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelLevel:
    """One control or status register: a name, a width, and the table a host
    widget decodes its value with."""

    name: str
    width: int
    enum: object | None = None

    def scalar(self):
        return self.width == 1

    def port_type(self):
        return "std_ulogic" if self.scalar() \
            else f"unsigned({self.width - 1} downto 0)"

    def spec(self):
        """The name-spec item naming this register: the item's width is the
        register's declared width, ascending so element 0 is the low bit."""
        base = self.name if self.scalar() \
            else f"{self.name}[0:{self.width - 1}]"
        return base if self.enum is None else base + self.enum.suffix()


@dataclass(frozen=True)
class TickWord:
    """One packed tick word: the ticks that strobe together."""

    names: tuple

    def count(self):
        return len(self.names)

    def spec(self):
        return ",".join(self.names)


@dataclass(frozen=True)
class PanelBody:
    """A whole panel: its clock, its counter width and its four kinds, each in
    description order, which is register order."""

    clock: str | None
    counter_width: int
    controls: tuple
    statuses: tuple
    tick_out: tuple
    tick_in: tuple

    COUNTER_WIDTH_DEFAULT = 32
    # Registers per 0x100-byte region, at one 32-bit word each.
    REGION_WORDS = 64
    # Ticks a word packs, and bits a level holds.
    WIDTH_MAX = 32

    def counter_count(self):
        """Tick inputs, word-major: one free-running counter each."""
        return sum(word.count() for word in self.tick_in)

    def levels(self):
        return self.controls + self.statuses

    def ticks(self):
        return self.tick_out + self.tick_in

    def names(self):
        """Every signal name of the panel, in description order."""
        names = [level.name for level in self.levels()]
        for word in self.ticks():
            names += list(word.names)
        return names

    def empty(self):
        return not self.names()

    def spec_of(self, levels):
        return ",".join(level.spec() for level in levels)

    def group_spec_of(self, words):
        """One word per group, groups separated by a semicolon: the word
        boundaries are the simultaneity groups, so they must survive into the
        descriptor."""
        return ";".join(word.spec() for word in words)
