"""``axi4_stream``: the command bytes arrive on the rack's own stream ports."""

from ..generator import (BusInterface, CommunicationRegistry, Constant,
                         Generic, Port, StreamGeometry)
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

    # The adapter carries one byte per beat with last, and nothing else:
    # that is its wire, not a geometry the instantiating design picks. The
    # packaged IP therefore states it rather than taking it as a parameter.
    GEOMETRY = StreamGeometry(data_bytes=1, last=True)

    @classmethod
    def vivado_interfaces(cls, context):
        params = {"config": cls.STREAM_CONFIG, "geometry": cls.GEOMETRY}
        return (BusInterface("s_axis", ("rx_i", "rx_o"), "slave",
                             clock=context.clock, params=params),
                BusInterface("m_axis", ("tx_o", "tx_i"), "master",
                             clock=context.clock, params=params))

    @classmethod
    def vivado_generics(cls, context):
        generics = {cls.STREAM_CONFIG: Constant(
            cls.STREAM_CONFIG, "nsl_amba.axi4_stream.config_t",
            cls.GEOMETRY.config(),
            comment="Both link streams, as the adapter speaks them.")}
        generics.update(super().vivado_generics(context))
        return generics

    @classmethod
    def deps(cls):
        return ("gatecap.adapter_stream", "nsl_amba.apb",
                "nsl_amba.axi4_stream")
