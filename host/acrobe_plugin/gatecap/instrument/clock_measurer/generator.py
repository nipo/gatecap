"""The ``!clock-measurer`` instrument, on the rack generator.

A description embeds the instrument under ``instruments``; this module turns
that entry into the ports, constants, instantiation and descriptor envelope of
a ``gatecap.clock_measurer.clock_rate_block`` inside the generated rack.

Everything the instrument needs is fixed by the description, so it contributes
no generic: the reference rate, the rate width, the update rate and the
address-space footprint become constants of the generated package, which both
the envelope and the instantiation are built from.

The reference clock is exported: another part of the rack -- the transport,
typically -- may run on it by naming ``<instance>.<reference>``.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (Constant, DescriptionError, Expr,
                                             Field, Instance, InstrumentPlugin,
                                             InstrumentRegistry, Port)


@InstrumentRegistry.register
class ClockMeasurer(InstrumentPlugin):
    """Measures several clocks against one reference clock of stated rate.

    Every clock named in the description becomes an input port of the
    generated rack, prefixed by the instance name: the reference clock and one
    port per observed clock, in description order, which is also the order of
    the rate registers and of the names in the descriptor."""

    TAG = "!clock-measurer"
    KEYS = ("reference", "frequency", "clocks", "max_rate", "update_hz_l2")

    UNIT = "gatecap.clock_measurer.clock_rate_block"
    ENVELOPE = "gatecap.clock_measurer.clock_measurer_envelope"
    SIZE_L2 = "gatecap.clock_measurer.clock_measurer_size_l2"
    DEP = "gatecap.clock_measurer"

    # One APB word per observed clock, so a rate has 32 bits to live in.
    RATE_WIDTH_MAX = 32
    # Rates refresh 2**update_hz_l2 times per second. Once per second is the
    # rate a measurement in Hz wants: the count is then the rate itself and
    # nothing is rounded. A simulation cannot afford a one-second window and
    # says so explicitly.
    UPDATE_HZ_L2_DEFAULT = 0

    @classmethod
    def parse(cls, payload, path):
        reference = cls.__clock_name(payload, "reference", path)
        frequency = cls.__positive(payload, "frequency", path)
        clocks = cls.__clocks(payload.get("clocks"), reference, path)
        max_rate = cls.__positive(payload, "max_rate", path)
        if max_rate >= 2**cls.RATE_WIDTH_MAX:
            raise DescriptionError(
                f"max_rate {max_rate} needs more than {cls.RATE_WIDTH_MAX} "
                "bits, and a rate is published in one APB word", path)
        update_hz_l2 = Field.integer(payload, "update_hz_l2", path,
                                     default=cls.UPDATE_HZ_L2_DEFAULT,
                                     minimum=0)
        # Both bounds the gateware asserts at elaboration, caught here instead
        # where the description can still be pointed at.
        rate_width = max_rate.bit_length()
        if rate_width <= update_hz_l2:
            raise DescriptionError(
                f"update_hz_l2 {update_hz_l2} leaves no counting bits in the "
                f"{rate_width}-bit rate max_rate {max_rate} asks for: a rate "
                "is a multiple of 2**update_hz_l2 Hz", path)
        if frequency // 2**update_hz_l2 < 2:
            raise DescriptionError(
                f"update_hz_l2 {update_hz_l2} makes the measurement window "
                f"shorter than two cycles of the {frequency} Hz reference "
                "clock", path)
        return {"reference": reference, "frequency": frequency,
                "clocks": clocks, "max_rate": max_rate,
                "rate_width": rate_width, "update_hz_l2": update_hz_l2}

    @staticmethod
    def __positive(payload, key, path):
        value = Field.integer(payload, key, path, minimum=1)
        if value is None:
            raise DescriptionError(f"{key} is required", path)
        return value

    @staticmethod
    def __clock_name(payload, key, path):
        """A clock name is half a port name, so it obeys the same rules."""
        name = Field.string(payload, key, path, required=True)
        return Field.identifier(name, path, key)

    @classmethod
    def __clocks(cls, value, reference, path):
        if not isinstance(value, list) or not value:
            raise DescriptionError(
                "clocks must be a non-empty list of clock names, got "
                f"{value!r}", path)
        clocks = []
        for entry in value:
            if not isinstance(entry, str):
                raise DescriptionError(
                    f"clocks must be clock names, got {entry!r}", path)
            name = Field.identifier(entry, path, "clock")
            if name == reference:
                raise DescriptionError(
                    f"clock {name!r} is the reference clock: the reference "
                    "measures the others and is not measured itself", path)
            if name in clocks:
                raise DescriptionError(
                    f"clock {name!r} is listed twice", path)
            clocks.append(name)
        return tuple(clocks)

    # Contributions

    @classmethod
    def ports(cls, instrument):
        params = instrument.params
        ports = [Port(instrument.port(f"{params['reference']}_i"), "in",
                      "std_ulogic",
                      comment=f"Reference clock, {params['frequency']} Hz "
                              "nominal: the time base every rate below is "
                              "measured against.")]
        for index, name in enumerate(params["clocks"]):
            ports.append(Port(
                instrument.port(f"{name}_i"), "in", "std_ulogic",
                comment="Observed clocks, in the order of the rate registers "
                        "and of the descriptor's name list."
                        if index == 0 else None))
        return tuple(ports)

    @classmethod
    def clocks(cls, instrument):
        reference = instrument.params["reference"]
        return {reference: instrument.port(f"{reference}_i")}

    @classmethod
    def clock_rates(cls, instrument):
        return {instrument.params["reference"]: instrument.params["frequency"]}

    @classmethod
    def constants(cls, context):
        instrument = context.instrument
        params = instrument.params
        quantum = 2**params["update_hz_l2"]
        return (
            Constant(instrument.constant("reference_hz"), "natural",
                     str(params["frequency"]),
                     comment="Nominal rate of the reference clock. The "
                             "measurement is a ratio, so this number scales "
                             "every published rate."),
            Constant(instrument.constant("rate_width"), "natural",
                     str(params["rate_width"]),
                     comment=f"Bits of an unsigned rate: what "
                             f"{params['max_rate']} Hz, the highest rate the "
                             "description expects, needs."),
            Constant(instrument.constant("update_hz_l2"), "natural",
                     str(params["update_hz_l2"]),
                     comment=f"Rates refresh {quantum} time(s) per second, "
                             f"and are multiples of {quantum} Hz."),
            Constant(instrument.constant("measured_count"), "natural",
                     str(len(params["clocks"]))),
            Constant(instrument.constant("measured_names"), "string",
                     Expr.string(",".join(params["clocks"])),
                     comment="Observed clocks in register order, as the "
                             "descriptor publishes them."),
            Constant(instrument.constant("size_l2"), "natural",
                     Expr.call(cls.SIZE_L2, context.data_bus_width_l2),
                     comment="Bytes of address space the instrument takes, "
                             "log2: what its register file decodes. The block "
                             "is given it and checks it against its own "
                             "decoding."),
            )

    @classmethod
    def instance(cls, context):
        instrument = context.instrument
        params = instrument.params
        port_map = {"clock_i": context.clock,
                    "reset_n_i": context.reset_n,
                    "apb_i": context.apb_master,
                    "apb_o": context.apb_slave,
                    "reference_clock_i":
                        instrument.port(f"{params['reference']}_i")}
        # Each clock is associated element by element: gathering them into a
        # vector signal first would put a delta cycle on every one of them.
        for index, name in enumerate(params["clocks"]):
            port_map[f"measured_clock_i({index})"] = \
                instrument.port(f"{name}_i")
        return Instance(
            instrument.label("measurer"), cls.UNIT,
            generic_map={
                "apb_config_c": context.apb_config,
                "size_l2_c": instrument.constant("size_l2"),
                "measured_count_c": instrument.constant("measured_count"),
                "reference_hz_c": instrument.constant("reference_hz"),
                "rate_width_c": instrument.constant("rate_width"),
                "update_hz_l2_c": instrument.constant("update_hz_l2"),
                "fingerprint_c": context.fingerprint},
            port_map=port_map,
            comment=f"Instrument {instrument.name}: "
                    f"{len(params['clocks'])} clock(s) measured against "
                    f"{params['reference']}.")

    @classmethod
    def envelope(cls, context):
        instrument = context.instrument
        return Expr.wrapped_call(
            cls.ENVELOPE,
            name=Expr.string(instrument.name),
            size_l2=instrument.constant("size_l2"),
            reference_name=Expr.string(instrument.params["reference"]),
            reference_hz=instrument.constant("reference_hz"),
            update_hz_l2=instrument.constant("update_hz_l2"),
            measured_names=instrument.constant("measured_names"))

    @classmethod
    def deps(cls, instrument):
        return (cls.DEP,)
