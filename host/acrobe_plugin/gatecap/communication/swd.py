"""``swd``: a two-wire serial-wire debug link into a Mem-AP."""

from ..generator import CommunicationPlugin, CommunicationRegistry, Port


@CommunicationRegistry.register
class SwdCommunication(CommunicationPlugin):
    """``swd``: two wires, and a stock SWD debug probe on the other end.

    The adapter carries a whole debug port; nothing above the wire is
    gatecap-specific, and the access port's identification register is what
    tells the host it is looking at a gatecap core, so no transport-level
    identify blob is involved: the descriptor sits at the bottom of the access
    port's memory space and the host reads it straight away."""

    MODE = "swd"
    UNIT = "gatecap.adapter_swd.swd_adapter"

    @classmethod
    def ports(cls):
        return (Port("swd_i", "in", "nsl_coresight.swd.swd_slave_i",
                     comment="Serial-wire debug pins: the clock and the data "
                             "line's input, output and direction."),
                Port("swd_o", "out", "nsl_coresight.swd.swd_slave_o"))

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_swd", "nsl_amba.apb", "nsl_coresight.swd")
