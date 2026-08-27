"""``spi``: four wires, and any SPI master on the other end."""

from ..generator import (CommunicationPlugin, CommunicationRegistry,
                         DescriptionError, Field, Generic, Port)


@CommunicationRegistry.register
class SpiCommunication(CommunicationPlugin):
    """``spi``: a memory-style SPI slave, driven by whatever master the board
    has -- a USB bridge, another FPGA, a microcontroller.

    One transaction per chip-select assertion, SPI-flash shaped: an opcode
    byte, a four-byte big-endian address, one dummy byte on reads, then data
    until the master releases the chip select. Nothing above the wire is
    gatecap-specific and no master capability is assumed: burst length is the
    master's own choice, and the host's alone to cap.

    The wire address is four bytes whatever the rack's footprint is. The
    footprint is in the descriptor, and the descriptor is read through this
    very address, so a wire format derived from it could not be spoken by a
    host that has not read it yet.

    The link has no backpressure -- the master clocks read data out at line
    rate -- so what makes it safe is the oversampling ratio: the slave samples
    SCK in the host clock domain, and the adapter asserts at elaboration that
    the host clock is at least ten times ``max_rate``. That is why the rate is
    a description key and not a wire parameter: it is the number the assert is
    made of."""

    MODE = "spi"
    UNIT = "gatecap.adapter_spi.spi_adapter"
    KEYS = ("max_rate", "spi_mode")
    MAX_RATE = "sck_max_rate_c"
    SPI_MODE = "spi_mode_c"
    CLOCK_FREQUENCY = "clock_frequency_c"

    @classmethod
    def parse(cls, section, path):
        max_rate = Field.integer(section, "max_rate", path, minimum=1)
        if max_rate is None:
            raise DescriptionError(
                "max_rate is required: the highest SCK rate the master will "
                "use is what the oversampling ratio is checked against",
                f"{path}.max_rate")
        return {"max_rate": max_rate,
                "spi_mode": Field.integer(section, "spi_mode", path,
                                          default=0, minimum=0, maximum=3)}

    @classmethod
    def ports(cls):
        return (Port("spi_i", "in", "nsl_spi.spi.spi_slave_i",
                     comment="SPI slave pins: chip select, clock and the two "
                             "data lines, the outgoing one with its output "
                             "enable."),
                Port("spi_o", "out", "nsl_spi.spi.spi_slave_o"))

    @classmethod
    def generics(cls, context):
        if context.clock_frequency:
            return ()
        return (Generic(
            cls.CLOCK_FREQUENCY, "natural",
            comment="Host clock rate in Hz. The description does not state "
                    "one -- the transport has a clock of its own, or the "
                    "domain it rides declares no frequency -- and the SPI "
                    "clock is only legal relative to it."),)

    @classmethod
    def clock_frequency(cls, context):
        """The rate as the adapter is given it: a literal when the description
        states one, the boundary generic otherwise."""
        if context.clock_frequency:
            return f"{context.clock_frequency:_}"
        return cls.CLOCK_FREQUENCY

    @classmethod
    def generic_map(cls, context):
        generic_map = {cls.APB_CONFIG: context.apb_config,
                       cls.CLOCK_FREQUENCY: cls.clock_frequency(context),
                       cls.MAX_RATE: f"{context.params['max_rate']:_}",
                       cls.SPI_MODE: str(context.params["spi_mode"]),
                       cls.DESCRIPTOR_BASE: context.descriptor_base}
        return generic_map

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_spi", "nsl_amba.apb", "nsl_spi.spi")
