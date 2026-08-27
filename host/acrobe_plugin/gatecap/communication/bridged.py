"""What every byte-stream transport shares: the stream-to-APB bridge."""

from ..generator import CommunicationPlugin, Generic


class BridgedCommunication(CommunicationPlugin):
    """Shared declarations of every transport that hands the core a byte
    stream.

    From the stream-to-APB bridge inwards the byte-stream modes are one
    design, and the burst length is a generic in all of them: it is the host's
    read budget, not a property of the wire."""

    BURST_LENGTH = "burst_length_l2_c"

    @classmethod
    def generics(cls, context):
        return (Generic(cls.BURST_LENGTH, "natural"),)

    @classmethod
    def generic_map(cls, context):
        return {cls.APB_CONFIG: context.apb_config,
                cls.BURST_LENGTH: cls.BURST_LENGTH,
                cls.DESCRIPTOR_BASE: context.descriptor_base}
