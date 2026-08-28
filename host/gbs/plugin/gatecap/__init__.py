"""gbs plugin turning a rack description into the library it describes.

A description is declared as a repository, and the units it stands for as
an ordinary dependency::

    repositories:
      - path: description.yaml
        loader: gatecap-description

    root:
      deps:
        - gatecap_generated.<rack package>

The repository (:mod:`.repository`) reads the description at project-load
time and publishes one partition of library `gatecap_generated` holding the
description itself; the partition's dependencies are the library partitions
the emitted code needs -- `gatecap.*`, `nsl_*` -- so dependency resolution
brings them in without the project naming any of them. The generation pass
then transpiles the description into the VHDL of that library, which a
hand-written unit reaches as `gatecap_generated.<rack package>`.

Known limitation: a rack is regenerated when its description file is
newer than the emitted VHDL. A change to the generator itself does not
trigger one, so clean the build directory after such a change.
"""

from gbs.base import BaseBackend, BasePass, BasePlugin


class GatecapGeneratePass(BasePass):
    """A description in, the rack's VHDL out.

    The pass carries no behaviour: it exists so the planner knows a
    `gatecap-description` source has a consumer producing VHDL. The work
    is scheduled by the generic dispatcher, which the plugin creates
    itself.
    """

    name = "gatecap-rack-generate"
    input_types = {"gatecap-description"}
    output_types = {"vhdl"}
    types_with_library = {"gatecap-description", "vhdl"}


class GatecapBackend(BaseBackend):
    """Contributes the generation pass to any plan wanting VHDL."""

    NAME = "gbs.plugin.gatecap"

    def __init__(self):
        super().__init__(self.NAME)

    def contribute_passes(self, config, output_types,
                          project_config=None, gbs_config=None):
        if "vhdl" not in output_types:
            return []
        return [GatecapGeneratePass(config, project_config, gbs_config)]


class GatecapPlugin(BasePlugin):
    """Registration entry point for the rack generator."""

    def __init__(self):
        super().__init__(
            name="gbs.plugin.gatecap",
            description="gatecap rack description to VHDL generator",
            version="0.1.0")

    def enumerate_backends(self):
        return [GatecapBackend()]

    def enumerate_repository_parsers(self):
        from .repository import GatecapDescriptionLoader
        return {GatecapDescriptionLoader.LOADER_NAME: GatecapDescriptionLoader}

    def generic_dispatchers(self, context):
        from .dispatcher import GatecapGenerateDispatcher
        return [GatecapGenerateDispatcher(context)]


def gbs_register():
    return GatecapPlugin()


__all__ = ["GatecapBackend", "GatecapGeneratePass", "GatecapPlugin",
           "gbs_register"]
