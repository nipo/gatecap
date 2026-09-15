"""What every byte-stream transport shares: the stream-to-APB bridge."""

from ..generator import CommunicationPlugin, Generic


class BridgedCommunication(CommunicationPlugin):
    """Shared declarations of every transport that hands the core a byte
    stream.

    From the stream-to-APB bridge inwards the byte-stream modes are one
    design, and the burst length is a generic in all of them: it is the host's
    read budget, not a property of the wire."""

    BURST_LENGTH = "burst_length_l2_c"
    # The budget a packaged IP is handed when nothing states one. A rack
    # instantiated by hand states it every time, so the rack generic has no
    # default; an IP parameter must have a value the block design elaborates
    # with, and 64 words is what a link of this shape reads comfortably.
    VIVADO_BURST_LENGTH = 6

    @classmethod
    def generics(cls, context):
        return (Generic(cls.BURST_LENGTH, "natural"),)

    @classmethod
    def vivado_generics(cls, context):
        return {cls.BURST_LENGTH: Generic(
            cls.BURST_LENGTH, "natural", default=str(cls.VIVADO_BURST_LENGTH),
            comment="Words the host may read in one command, log2.")}

    @classmethod
    def generic_map(cls, context):
        return {cls.APB_CONFIG: context.apb_config,
                cls.BURST_LENGTH: cls.BURST_LENGTH,
                cls.DESCRIPTOR_BASE: context.descriptor_base}
