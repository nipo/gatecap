"""``axi4_stream``: the command bytes arrive on the rack's own stream ports."""

from ..generator import CommunicationRegistry, Generic, Port
from .bridged import BridgedCommunication


@CommunicationRegistry.register
class Axi4StreamCommunication(BridgedCommunication):
    """``axi4_stream``: the stream adapter fed by the entity's own stream
    ports. The design instantiating the core owns the transport carrying
    them."""

    MODE = "axi4_stream"
    UNIT = "gatecap.adapter_stream.stream_adapter"
    STREAM_CONFIG = "stream_config_c"

    @classmethod
    def ports(cls):
        return (Port("rx_i", "in", "nsl_amba.axi4_stream.master_t"),
                Port("rx_o", "out", "nsl_amba.axi4_stream.slave_t"),
                Port("tx_o", "out", "nsl_amba.axi4_stream.master_t"),
                Port("tx_i", "in", "nsl_amba.axi4_stream.slave_t"))

    @classmethod
    def generics(cls, context):
        return ((Generic(cls.STREAM_CONFIG,
                         "nsl_amba.axi4_stream.config_t"),)
                + super().generics(context))

    @classmethod
    def generic_map(cls, context):
        generic_map = {cls.APB_CONFIG: context.apb_config,
                       cls.STREAM_CONFIG: cls.STREAM_CONFIG}
        generic_map.update(super().generic_map(context))
        return generic_map

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_stream", "nsl_amba.apb",
                "nsl_amba.axi4_stream")
