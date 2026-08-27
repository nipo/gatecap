"""Description parsing and validation.

A description names the rack, its transport and its instruments; everything
below an instrument is validated by the instrument's own plugin, which is
handed its body and the dotted path to report against. What is decided here is
what the framework owns: the rack's name, the transport and where its clock
comes from, the instance names, and the boundary ports the instruments claim.

Everything decidable from the description alone is checked before any VHDL
exists. Geometry that depends on an entity generic is not knowable in Python
and becomes an elaboration-time assertion in the generated code instead.
"""

from __future__ import annotations

from .communication import CommunicationRegistry, HostClock
from .errors import DescriptionError
from .fields import Field
from .instruments import InstrumentRegistry
from .loader import Tagged, YamlSource
from .schema import Communication, Description, Instrument, Name


class DescriptionParser:
    """YAML document -> validated :class:`Description`."""

    TOP_KEYS = ("name", "communication", "instruments")
    COMMUNICATION_KEYS = ("mode", "clock")

    @classmethod
    def load_file(cls, path):
        return cls.parse(YamlSource.load_file(path))

    @classmethod
    def load(cls, text, origin=None):
        return cls.parse(YamlSource.load(text, origin))

    @classmethod
    def parse(cls, document):
        Field.known_keys(document, cls.TOP_KEYS, "")
        name = Name.parse(document.get("name"), "name")
        instruments = cls.__instruments(
            Field.mapping(document, "instruments", ""))
        if not instruments:
            raise DescriptionError("a rack needs at least one instrument")
        communication = cls.__communication(
            Field.mapping(document, "communication", "", required=True),
            instruments)

        description = Description(name=name, communication=communication,
                                  instruments=instruments)
        cls.__check_ports(description)
        return description

    @classmethod
    def __communication(cls, section, instruments):
        path = "communication"
        mode = Field.string(section, "mode", path, required=True)
        plugin = CommunicationRegistry.get(mode, f"{path}.mode")
        # The mode is what says which keys the section may hold, so it is read
        # before they are checked.
        Field.known_keys(section, cls.COMMUNICATION_KEYS + plugin.KEYS, path)
        params = plugin.parse(section, path)
        clock = Field.string(section, "clock", path)
        plugin.validate(clock, f"{path}.clock")
        if clock is None:
            return Communication(mode=mode, plugin=plugin, params=params)
        return Communication(
            mode=mode, plugin=plugin, params=params,
            clock_export=cls.__clock_export(clock, instruments,
                                            f"{path}.clock"))

    @staticmethod
    def __clock_export(clock, instruments, path):
        """The key holds ``<instance>.<clock>``, one of the clocks an
        instrument exports."""
        exports = {}
        for instrument in instruments:
            for name in instrument.plugin.clocks(instrument):
                exports[f"{instrument.name}.{name}"] = instrument
        if clock in exports:
            return clock
        known = (f"known: {', '.join(sorted(exports))}" if exports
                 else "no instrument of this rack exports one")
        raise DescriptionError(
            f"clock {clock!r} is not an exported clock "
            f"(a rack names one as <instance>.<clock>; {known})", path)

    @classmethod
    def __instruments(cls, section):
        instruments = []
        seen = set()
        for name, body in section.items():
            instrument = cls.__instrument(name, body)
            if instrument.name in seen:
                raise DescriptionError(f"instrument {name!r} is declared twice",
                                       "instruments")
            seen.add(instrument.name)
            instruments.append(instrument)
        return tuple(instruments)

    @classmethod
    def __instrument(cls, name, body):
        path = f"instruments.{name}"
        Field.identifier(name, "instruments", "instrument name")
        line = YamlSource.line_of(body)
        if not isinstance(body, Tagged):
            raise DescriptionError(
                "an instrument entry is tagged with the instrument it holds "
                f"({InstrumentRegistry.known()})", path)
        plugin = InstrumentRegistry.get(body.tag, path, line)
        payload = body.mapping(path)
        Field.known_keys(payload, plugin.KEYS, path)
        return Instrument(name=name, tag=body.tag, plugin=plugin,
                          params=plugin.parse(payload, path))

    @classmethod
    def __check_ports(cls, description):
        owners = {}
        for port in HostClock.resolve(description).ports:
            owners[port.name] = "the host clock"
        for port in description.communication.plugin.ports():
            owners[port.name] = \
                f"communication mode {description.communication.mode}"
        for instrument in description.instruments:
            for port in instrument.plugin.ports(instrument):
                cls.__claim(owners, port.name,
                            f"instrument {instrument.name}")

    @staticmethod
    def __claim(owners, name, owner):
        if name in owners:
            raise DescriptionError(
                f"port {name} is claimed by both {owners[name]} and {owner}")
        owners[name] = owner
