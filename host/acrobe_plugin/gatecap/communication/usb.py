"""``usb``: a USB Full Speed device, and any USB host on the other end."""

from ..generator import CommunicationRegistry, DescriptionError, Port
from .bridged import BridgedCommunication


@CommunicationRegistry.register
class UsbCommunication(BridgedCommunication):
    """``usb``: the rack is a USB Full Speed device of its own.

    The whole stack is fabric -- phy included -- so the link costs three IOs
    (D+, D-, and one driving the D+ pullup through a resistor) and a reference
    clock. That clock is the rack's host clock too, which is why the mode
    accepts only the rates the phy recovers bits with.

    Identity is on the bus: vendor 0x1500, product 0xdeca, one vendor-defined
    interface, and the rack's descriptor fingerprint as the serial-number
    string. A host enumerates a gatecap rack and knows which one it is before
    opening anything, so nothing about the connection has to be spelled out by
    hand.

    The interface holds one bulk endpoint pair, framed by short packets: one
    datagram is one bridge command or one bridge response. From there inwards
    it is the same stream-to-APB bridge every byte-stream transport ends in."""

    MODE = "usb"
    UNIT = "gatecap.adapter_usb.usb_adapter"
    FINGERPRINT = "fingerprint_c"
    CLOCK_FREQUENCY = "clock_frequency_c"

    # Reference clock rates the Full Speed phy has a recovery loop for.
    CLOCK_RATES = (48_000_000, 60_000_000)

    @classmethod
    def check(cls, context):
        if context.clock_frequency and \
           context.clock_frequency not in cls.CLOCK_RATES:
            rates = ", ".join(f"{rate // 1_000_000} MHz"
                              for rate in cls.CLOCK_RATES)
            raise DescriptionError(
                f"the usb transport runs on the phy's reference clock "
                f"({rates}), and the clock it rides is stated at "
                f"{context.clock_frequency / 1e6:g} MHz",
                "communication.clock")

    @classmethod
    def ports(cls):
        return (Port("usb_o", "out", "nsl_usb.io.usb_io_c",
                     comment="Line drive: D+, D-, the driver's output enable "
                             "and the D+ pullup's. The pullup is how the "
                             "device announces itself, so it is part of the "
                             "link and not of the board."),
                Port("usb_i", "in", "nsl_usb.io.usb_io_s"),
                Port("online_o", "out", "std_ulogic",
                     comment="The host has configured the device. A LED's "
                             "worth of status; the rack does not depend on "
                             "it."))

    @classmethod
    def generic_map(cls, context):
        generic_map = {cls.APB_CONFIG: context.apb_config,
                       cls.FINGERPRINT: context.fingerprint}
        if context.clock_frequency:
            generic_map[cls.CLOCK_FREQUENCY] = f"{context.clock_frequency:_}"
        generic_map.update(super().generic_map(context))
        return generic_map

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_usb", "nsl_amba.apb", "nsl_usb.io")
