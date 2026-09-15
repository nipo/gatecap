"""``serial_hdlc``: HDLC frames over an 8n1 UART."""

from ..generator import CommunicationRegistry, Generic, Port
from .bridged import BridgedCommunication


@CommunicationRegistry.register
class SerialHdlcCommunication(BridgedCommunication):
    """``serial_hdlc``: two wires, an 8n1 UART carrying HDLC frames.

    The UART needs its bit period in host-clock cycles, so the adapter takes
    the host clock rate: the description states it when the transport rides a
    domain that declares one, and it is a generic otherwise."""

    MODE = "serial_hdlc"
    UNIT = "gatecap.adapter_serial_hdlc.serial_hdlc_adapter"
    BAUD_RATE = "baud_rate_c"
    CLOCK_FREQUENCY = "clock_frequency_c"

    @classmethod
    def ports(cls):
        return (Port("uart_rx_i", "in", "std_ulogic"),
                Port("uart_tx_o", "out", "std_ulogic"))

    @classmethod
    def generics(cls, context):
        generics = ()
        if not context.clock_frequency:
            generics += (Generic(
                cls.CLOCK_FREQUENCY, "natural",
                comment="Host clock rate in Hz. The description does not "
                        "state one -- the transport has a clock of its own, "
                        "or the domain it rides declares no frequency -- and "
                        "the baud rate is meaningless without it."),)
        generics += (Generic(
            cls.BAUD_RATE, "natural",
            comment="Bits per second on the line, 8n1. No default: the wire "
                    "rate is the one thing the two ends must agree on."),)
        return generics + super().generics(context)

    # The rate a packaged IP starts at. The rack generic has no default on
    # purpose, but an IP parameter must carry a value, and 8n1 at this rate is
    # what a host opens a port at when nothing says otherwise.
    VIVADO_BAUD_RATE = 115200

    @classmethod
    def vivado_generics(cls, context):
        generics = {cls.BAUD_RATE: Generic(
            cls.BAUD_RATE, "natural", default=str(cls.VIVADO_BAUD_RATE),
            comment="Bits per second on the line, 8n1.")}
        generics.update(super().vivado_generics(context))
        return generics

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
                       cls.BAUD_RATE: cls.BAUD_RATE}
        generic_map.update(super().generic_map(context))
        return generic_map

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_serial_hdlc", "nsl_amba.apb")
