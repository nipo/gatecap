"""Instrument plugins, keyed by the YAML tag of an ``instruments`` entry.

An instrument is a user-facing feature of a rack -- a logic analyzer, a
clock-rate measurer: one entry in the description, one entity with a single
APB port, one node with children on the host side. A plugin owns one
instrument type. Each ``instruments`` entry names an instance -- the key is
the instance name, the tag selects the plugin -- and the plugin answers for
it.

A contribution is one entity, instantiated:

* the instantiation of the instrument's entity, plus any file the plugin
  emits into the output directory when the instrument is generated rather
  than taken from a library;
* rack-boundary ports, prefixed with the instance name, and generics to
  forward;
* the constants the generated package declares for the instance, and the
  descriptor envelope as a VHDL expression built on ``instrument_envelope``.
  The envelope and the entity are written by one plugin from one set of
  generics, so they cannot disagree;
* exported clocks: a name per boundary port carrying a clock the rest of the
  rack may run on, referenced from the description as ``<instance>.<clock>``;
* gbs partition dependencies.

An instrument never learns its own base: it sees its APB slice and nothing
else. Its address-space footprint is the ``size_l2`` of its envelope, which
the backplane reads back from the envelope itself, and which the instrument
entity asserts against the address bits it really decodes.

Everything internal a plugin touches -- the APB configuration constant, the
two APB signals of its segment, the host clock, the fingerprint -- reaches it
through an :class:`InstrumentContext`, so a plugin never has to know how the
address map was laid out.

gatecap ships four instruments -- the logic analyzer, the control/status
panel, the clock measurer and the bus explorer; the mechanism is the same for
third-party ones, which register from their own acrobe plugin at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DescriptionError


@dataclass(frozen=True)
class InstrumentContext:
    """What the rack offers an instrument plugin."""

    instrument: object    # the Instrument entry this contribution is for
    clock: str            # host clock signal or port name
    reset_n: str          # host reset name
    apb_config: str       # expression naming the APB config constant
    apb_master: str       # the segment's requester side, towards the entity
    apb_slave: str        # the segment's completer side, back to the router
    data_bus_width_l2: str  # constant holding the bytes per APB word, log2
    fingerprint: str      # constant holding the rack's descriptor fingerprint
    clock_frequency: int  # host clock in Hz, 0 when the description omits it
    entity: str           # the rack entity, to name whatever the plugin emits
    host_clock: str | None  # this instance's exported clock the rack itself
                            # runs on, so an instrument sharing it with one of
                            # its own domains knows the two are one domain;
                            # None when the rack has a clock of its own
    size_l2: str          # expression: the footprint read back from the
                          # envelope, for the instrument to check its own
                          # decoding against


@dataclass(frozen=True)
class InstrumentContribution:
    # An instrument the host cannot enumerate is not an instrument, and an
    # instrument that is not instantiated is nothing at all, so the envelope
    # and the instance are the two parts of a contribution with no default.
    envelope: str
    instance: object
    ports: tuple = ()
    generics: tuple = ()
    constants: tuple = ()
    # Declarations local to the envelope function the rack wraps the
    # expression in: geometry an envelope built from generics needs, which no
    # package constant can hold.
    envelope_declarations: tuple = ()
    # Component declarations the generated package carries for whatever the
    # plugin emitted, so the backplane instantiates it like any other unit.
    components: tuple = ()
    clocks: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    deps: tuple = ()


class InstrumentPlugin:
    """Base class and contribution interface of an instrument type."""

    # YAML tag selecting the plugin, as written in the description.
    TAG = None
    # Keys the instance body accepts.
    KEYS = ()
    @classmethod
    def parse(cls, payload, path):
        """Validate the instance's keys and return them as its params."""
        return {}

    @classmethod
    def ports(cls, instrument):
        """Rack-boundary ports this instance adds, named after it."""
        raise NotImplementedError

    @classmethod
    def generics(cls, instrument):
        """Rack-boundary generics this instance adds, named after it."""
        return ()

    @classmethod
    def clocks(cls, instrument):
        """Exported clocks: name -> the boundary port carrying it. A clock is
        bound structurally where it is used, never re-driven through a signal
        assignment."""
        return {}

    @classmethod
    def clock_rates(cls, instrument):
        """Rates of the exported clocks the description states, in Hz. A
        transport riding an exported clock and needing its rate -- a UART
        dividing it down to a baud rate -- takes it from here instead of
        asking for a generic."""
        return {}

    @classmethod
    def constants(cls, context):
        """Constants the generated package declares for this instance. Both
        the envelope and the instantiation are built from them, and a package
        declaration is what the two entities share."""
        return ()

    @classmethod
    def envelope_declarations(cls, context):
        """Declarations local to the function the rack builds the envelope
        expression in. An envelope whose geometry follows from the instance's
        generics cannot be a package constant, so it is a function of them,
        and this is that function's declarative part."""
        return ()

    @classmethod
    def components(cls, context):
        """Component declarations the generated package carries. A plugin
        emitting an entity of its own declares it here, so the backplane
        instantiates it the way it instantiates anything else."""
        return ()

    @classmethod
    def envelope(cls, context):
        """VHDL byte_string expression: the instance's descriptor envelope."""
        raise NotImplementedError

    @classmethod
    def instance(cls, context):
        """The instantiation of this instance's entity."""
        raise NotImplementedError

    @classmethod
    def files(cls, context):
        """Extra files the plugin writes next to the generated rack, as
        name -> contents, in analysis order. Empty for an instrument taken
        from a library."""
        return {}

    @classmethod
    def deps(cls, instrument):
        return ()

    @classmethod
    def contribute(cls, context):
        instrument = context.instrument
        return InstrumentContribution(
            envelope=cls.envelope(context),
            instance=cls.instance(context),
            ports=tuple(cls.ports(instrument)),
            generics=tuple(cls.generics(instrument)),
            constants=tuple(cls.constants(context)),
            envelope_declarations=tuple(cls.envelope_declarations(context)),
            components=tuple(cls.components(context)),
            clocks=dict(cls.clocks(instrument)),
            files=dict(cls.files(context)),
            deps=tuple(cls.deps(instrument)))


class InstrumentRegistry:
    """YAML tag -> instrument plugin."""

    PLUGINS = {}

    @classmethod
    def register(cls, plugin):
        assert plugin.TAG not in cls.PLUGINS, \
            f"instrument {plugin.TAG!r} is already registered"
        cls.PLUGINS[plugin.TAG] = plugin
        return plugin

    @classmethod
    def get(cls, tag, path, line=None):
        try:
            return cls.PLUGINS[tag]
        except KeyError:
            raise DescriptionError(
                f"unknown instrument {tag!r} ({cls.known()})",
                path, line) from None

    @classmethod
    def tags(cls):
        return tuple(cls.PLUGINS)

    @classmethod
    def known(cls):
        if not cls.PLUGINS:
            return "none is registered"
        return f"known: {', '.join(cls.tags())}"
