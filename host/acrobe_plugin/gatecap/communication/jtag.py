"""``jtag``: the command bytes ride the FPGA's own TAP."""

from ..generator import CommunicationRegistry, Port
from .bridged import BridgedCommunication


@CommunicationRegistry.register
class JtagCommunication(BridgedCommunication):
    """``jtag``: the host reaches the core through the FPGA's own TAP.

    Only the chip TAP pins and the host clock show up on the core: the
    transport crosses TCK to the host clock in its own FIFOs, so this mode is
    clocked like any other and takes the usual ``clock`` key.

    The TAP pins are ports because some vendors expect the boundary pins
    routed to the primitive explicitly; the ones that wire the TAP internally
    leave them unbound, which the defaults of the adapter allow."""

    MODE = "jtag"
    UNIT = "gatecap.adapter_jtag.jtag_adapter"

    @classmethod
    def ports(cls):
        return (Port("chip_tck_i", "in", "std_ulogic", default="'0'",
                     comment="TAP pins, tied off by default: a vendor whose "
                             "TAP primitive reaches the boundary on its own "
                             "leaves them unbound."),
                Port("chip_tms_i", "in", "std_ulogic", default="'0'"),
                Port("chip_tdi_i", "in", "std_ulogic", default="'0'"),
                Port("chip_tdo_o", "out", "std_ulogic"))

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_jtag", "nsl_amba.apb")
