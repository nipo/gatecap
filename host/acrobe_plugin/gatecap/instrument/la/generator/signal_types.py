"""The signal types a description can probe: gatecap's own probe vocabulary.

Each is a :class:`SignalTypePlugin` keyed by the YAML tag of a signal entry,
registered when this package is imported.

Abstract types are backed by a gateware packer package exposing the
``*_length`` / ``*_pack`` / ``*_names`` triad over one element-key string, so
vector layout and name-spec are computed by the same VHDL code and cannot
drift from the generator.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (DescriptionError, Expr, Generic,
                                             Port, SignalTypePlugin,
                                             SignalTypeRegistry)


@SignalTypeRegistry.register
class ScalarSignal(SignalTypePlugin):
    """The untagged entry: one std_ulogic probe."""

    TAG = None

    @classmethod
    def ports(cls, probe):
        return (Port(probe.port_name(), "in", "std_ulogic"),)

    @classmethod
    def length(cls, probe, selection):
        return "1"

    @classmethod
    def pack(cls, probe, selection):
        return Expr.scalar_vector(probe.port_name())

    @classmethod
    def names(cls, probe, selection):
        return Expr.string(probe.name + cls.enum_suffix(probe))

    @classmethod
    def static_width(cls, probe, selection):
        return 1


@SignalTypeRegistry.register
class BusSignal(SignalTypePlugin):
    """``!bus``: a std_ulogic_vector of a width fixed by the description.

    Bus widths are never entity generics: the host-visible geometry of a bus
    is decided here, not by the instantiating design."""

    TAG = "!bus"
    KEYS = ("width",)

    @classmethod
    def parse(cls, payload, path):
        if "width" not in payload:
            raise DescriptionError("!bus requires a width", path)
        width = payload["width"]
        if not isinstance(width, int) or isinstance(width, bool) or width < 1:
            raise DescriptionError(
                f"!bus width must be a positive integer, got {width!r}", path)
        return {"width": width}

    @classmethod
    def ports(cls, probe):
        width = probe.params["width"]
        return (Port(probe.port_name(), "in",
                     f"std_ulogic_vector({width - 1} downto 0)"),)

    @classmethod
    def length(cls, probe, selection):
        return str(probe.params["width"])

    @classmethod
    def pack(cls, probe, selection):
        return probe.port_name()

    @classmethod
    def names(cls, probe, selection):
        # Ascending, so element 0 -- the vector LSB -- is the low probe bit.
        width = probe.params["width"]
        return Expr.string(
            f"{probe.name}[0:{width - 1}]" + cls.enum_suffix(probe))

    @classmethod
    def static_width(cls, probe, selection):
        return probe.params["width"]


class ElementSelectedSignal(SignalTypePlugin):
    """A whole abstract bus, selected field by field.

    The type's alphabet names the fields its gateware packer knows; a trace or
    trigger marking is an ordered selection of letters over that alphabet, and
    the whole alphabet is what a trace defaults to. The packer names the fields
    itself, so no enum can attach to such a probe."""

    ENUM = False
    ALPHABET = None

    @classmethod
    def parse_trace(cls, value, path):
        if value is None:
            return True, cls.ALPHABET
        if value is False:
            return False, None
        return True, cls.elements(value, "trace", path)

    @classmethod
    def parse_trigger(cls, value, path):
        if value is None or value is False:
            return None
        return cls.elements(value, "trigger", path)

    @classmethod
    def elements(cls, value, key, path):
        if not isinstance(value, str) or not value:
            raise DescriptionError(
                f"{key} for {cls.TAG} must be a non-empty element string over "
                f"[{cls.ALPHABET}], got {value!r}", path)
        seen = set()
        for letter in value:
            if letter not in cls.ALPHABET:
                raise DescriptionError(
                    f"{key} element {letter!r} is not one of "
                    f"[{cls.ALPHABET}]", path)
            if letter in seen:
                raise DescriptionError(
                    f"{key} element {letter!r} is repeated", path)
            seen.add(letter)
        return value


@SignalTypeRegistry.register
class Axi4StreamSignal(ElementSelectedSignal):
    """``!axi4-stream``: a whole AXI4-Stream bus, packed by the gateware.

    The stream configuration stays out of the description: the entity gains a
    ``config_t`` generic with no default, deliberately, so a mismatch between
    the probed bus and the capture geometry cannot hide behind a default that
    happens to elaborate."""

    TAG = "!axi4-stream"
    ALPHABET = "idskouvlr"
    PACKAGE = "gatecap.axi4_stream_packer"

    @classmethod
    def config_generic(cls, probe):
        return f"{probe.scope()}_{probe.name}_config_c"

    @classmethod
    def ports(cls, probe):
        return (Port(probe.port_name(), "in", "nsl_amba.axi4_stream.bus_t"),)

    @classmethod
    def generics(cls, probe):
        return (Generic(cls.config_generic(probe),
                        "nsl_amba.axi4_stream.config_t"),)

    @classmethod
    def deps(cls, probe):
        return ("nsl_amba.axi4_stream", "gatecap.axi4_stream_packer")

    @classmethod
    def length(cls, probe, selection):
        return Expr.call(f"{cls.PACKAGE}.axis_length",
                         cls.config_generic(probe), Expr.string(selection))

    @classmethod
    def pack(cls, probe, selection):
        return Expr.call(f"{cls.PACKAGE}.axis_pack",
                         cls.config_generic(probe), Expr.string(selection),
                         probe.port_name())

    @classmethod
    def names(cls, probe, selection):
        # A brace group prefixes every packed field with the signal name, which
        # the host turns into a VCD scope.
        return Expr.concat(
            Expr.string(probe.name + ".{"),
            Expr.call(f"{cls.PACKAGE}.axis_names",
                      cls.config_generic(probe), Expr.string(selection)),
            Expr.string("}"))


class BnocSignal(ElementSelectedSignal):
    """Common part of the bnoc bus types.

    A bnoc bus has one fixed geometry -- an 8-bit data byte plus handshake
    lines -- so, unlike an AXI4-Stream probe, it carries no configuration
    generic and every selection has a width Python can compute. The family
    name spells the record, the gbs partition and the packer functions alike:
    ``nsl_bnoc.framed.framed_bus_t``, ``framed_pack`` and friends."""

    PACKAGE = "gatecap.bnoc_packer"
    ELEMENT_WIDTHS = {"d": 8, "v": 1, "l": 1, "r": 1}
    FAMILY = None

    @classmethod
    def record(cls):
        return f"nsl_bnoc.{cls.FAMILY}.{cls.FAMILY}_bus_t"

    @classmethod
    def function(cls, what):
        return f"{cls.PACKAGE}.{cls.FAMILY}_{what}"

    @classmethod
    def ports(cls, probe):
        return (Port(probe.port_name(), "in", cls.record()),)

    @classmethod
    def deps(cls, probe):
        return (f"nsl_bnoc.{cls.FAMILY}", cls.PACKAGE)

    @classmethod
    def length(cls, probe, selection):
        return Expr.call(cls.function("length"), Expr.string(selection))

    @classmethod
    def pack(cls, probe, selection):
        return Expr.call(cls.function("pack"), probe.port_name(),
                         Expr.string(selection))

    @classmethod
    def names(cls, probe, selection):
        # A brace group prefixes every packed field with the signal name, which
        # the host turns into a VCD scope.
        return Expr.concat(
            Expr.string(probe.name + ".{"),
            Expr.call(cls.function("names"), Expr.string(selection)),
            Expr.string("}"))

    @classmethod
    def static_width(cls, probe, selection):
        if selection is None:
            return None
        return sum(cls.ELEMENT_WIDTHS[letter] for letter in selection)


@SignalTypeRegistry.register
class BnocFramedSignal(BnocSignal):
    """``!bnoc-framed``: a bnoc framed bus, data byte plus frame boundary."""

    TAG = "!bnoc-framed"
    FAMILY = "framed"
    ALPHABET = "dvlr"


@SignalTypeRegistry.register
class BnocPipeSignal(BnocSignal):
    """``!bnoc-pipe``: a bnoc pipe bus, a byte stream with no framing."""

    TAG = "!bnoc-pipe"
    FAMILY = "pipe"
    ALPHABET = "dvr"
