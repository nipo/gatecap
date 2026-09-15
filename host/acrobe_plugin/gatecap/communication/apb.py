"""``apb``: no adapter, the rack's own APB completer is its port pair."""

from ..generator import (BusInterface, CommunicationPlugin,
                         CommunicationRegistry, Port)


@CommunicationRegistry.register
class ApbCommunication(CommunicationPlugin):
    """``apb``: no adapter, the internal APB completer is the unit's port.

    The APB configuration is the one the unit derives from its own geometry,
    and the instantiating design must drive a matching requester."""

    MODE = "apb"
    INTERFACE = "s_apb"

    @classmethod
    def ports(cls):
        return (Port("apb_i", "in", "nsl_amba.apb.master_t"),
                Port("apb_o", "out", "nsl_amba.apb.slave_t"))

    @classmethod
    def vivado_interfaces(cls, context):
        # The completer the rack offers, on the configuration it derived its
        # own map from -- which the wrapper carries as the constant the
        # boundary generic defaults to.
        return (BusInterface(
            cls.INTERFACE, ("apb_i", "apb_o"), "slave",
            clock=context.clock,
            params={"geometry": context.apb_geometry}),)

    @classmethod
    def generic_map(cls, context):
        return {}

    @classmethod
    def deps(cls):
        return ("nsl_amba.apb",)
