"""Intermediate representation of a logic-analyzer instrument body.

The plugin's parser turns the ``!logic-analyzer`` mapping into these frozen
dataclasses; the cluster, topology and emission code reads them and never the
raw YAML.

An enumeration table is the framework's :class:`EnumSpec`, re-exported here:
the ``<...>`` name-spec suffix it renders is the descriptor's grammar, not
this instrument's.

A domain and a probe carry the name of the instrument instance they belong to,
because that name prefixes every boundary port and generic: two analyzers in
one rack may both hold a domain called ``control``, and their ports must
still differ.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from acrobe_plugin.gatecap.generator import EnumSpec


@dataclass(frozen=True)
class StorageParams:
    buffer_depth_l2: int
    rle: bool
    packed: bool

    DEFAULTS = {"buffer_depth_l2": 10, "rle": False, "packed": False}


@dataclass(frozen=True)
class CaptureParams:
    max_windows: int

    DEFAULTS = {"max_windows": 1}


@dataclass(frozen=True)
class TriggerParams:
    capabilities: str

    CAPABILITIES = ("value", "edge")
    DEFAULTS = {"capabilities": "value"}

    def edge(self):
        return self.capabilities == "edge"


@dataclass(frozen=True)
class Probe:
    """One described signal, with its type plugin and its markings."""

    domain: str
    name: str
    tag: str
    plugin: type
    traced: bool
    trace_selection: str | None
    trigger_selection: str | None
    enum: EnumSpec | None
    params: dict
    instrument: str = ""

    def triggered(self):
        return self.trigger_selection is not None

    def scope(self):
        """What names the probe on the rack boundary: the instance and the
        domain it belongs to."""
        return f"{self.instrument}_{self.domain}" if self.instrument \
            else self.domain

    def port_name(self):
        return f"{self.scope()}_{self.name}_i"

    def path(self):
        return f"{self.__instrument_path()}domains.{self.domain}." \
               f"signals.{self.name}"

    def __instrument_path(self):
        return f"instruments.{self.instrument}." if self.instrument else ""


@dataclass(frozen=True)
class Domain:
    """One capture clock domain: its clock, its dimensioning and its probes."""

    name: str
    clock: str
    frequency: int
    storage: StorageParams
    capture: CaptureParams
    trigger: TriggerParams
    trigger_from: str | None
    probes: tuple
    instrument: str = ""

    def scope(self):
        return f"{self.instrument}_{self.name}" if self.instrument else self.name

    def clock_port(self):
        return f"{self.scope()}_{self.clock}_i"

    def reset_port(self):
        return f"{self.scope()}_reset_n_i"

    def traced_probes(self):
        return tuple(p for p in self.probes if p.traced)

    def trigger_probes(self):
        return tuple(p for p in self.probes if p.triggered())

    def captures(self):
        return bool(self.traced_probes())

    def hosts_trigger(self):
        return bool(self.trigger_probes())

    def subscribes(self):
        return self.trigger_from is not None

    def scoped(self, instrument):
        """The same domain, stamped with the instance that holds it."""
        return replace(
            self, instrument=instrument,
            probes=tuple(replace(probe, instrument=instrument)
                         for probe in self.probes))


class Defaults:
    """Merge of an instrument-level dimensioning section with a domain
    override."""

    @staticmethod
    def merged(base, override):
        if not override:
            return base
        return replace(base, **override)
