"""A rack description seen as a gbs repository.

A description is a whole library's worth of VHDL, so a project declares it
the way it declares any other source of units it does not own::

    repositories:
      - path: description.yaml
        loader: gatecap-description

    root:
      deps:
        - gatecap_generated.<rack package>

The repository holds one library, ``gatecap_generated``, with one partition
named after the rack's VHDL package. That partition's source is the
description itself, typed `gatecap-description`, and its dependencies are the
library partitions the emitted code needs. Ordinary dependency resolution
then brings the description and everything under it into the build in one
topologically ordered pass, and the generation pass turns the description into
the VHDL of the partition's library.

A second partition adds the topcell a Vivado IP is packaged from. It is named
after that topcell, so the ``deps`` entry and the output group's ``topcell``
cannot disagree. Naming it is how a project asks for the topcell; a project
that never packages an IP never mentions it and never pays for it::

    root_library_name: gatecap_generated
    root:
      name: <rack entity>_vivado_ip
      deps:
        - gatecap_generated.<rack entity>_vivado_ip

The description is parsed here, at project-load time: a description that does
not hold up is a project error, reported before anything is planned.
"""

from pathlib import Path

from gbs.repository.loader import LoadError, RepositoryLoader
from gbs.repository.model import Partition, Repository, SourceFile


class GatecapDescriptionRepository(Repository):
    """The one library one partition a description amounts to."""

    LIBRARY = "gatecap_generated"
    FILE_TYPE = "gatecap-description"
    VIVADO_IP_SUFFIX = "_vivado_ip"

    def __init__(self, name, path, package, entity, deps, vivado_ip_deps):
        super().__init__(name, path.parent)
        self.description_path = path
        self.package = package
        self.entity = entity
        self.partition_deps = frozenset(deps)
        self.vivado_ip_deps = frozenset(vivado_ip_deps)

    def partition_name(self):
        return f"{self.LIBRARY}.{self.package}"

    def vivado_ip_partition_name(self):
        """Named after the topcell it holds, not after the package: the
        project states that same name as its output group's topcell, and one
        word cannot disagree with itself."""
        return f"{self.LIBRARY}.{self.entity}{self.VIVADO_IP_SUFFIX}"

    def file_types(self):
        """A description is the only source this repository has, and it has
        it whatever the build asks for: the planner needs a consumer for the
        type in every plan the repository takes part in."""
        return {self.FILE_TYPE}

    def partition_lookup(self, partition_name, filter_vars):
        if partition_name == self.partition_name():
            return self.__partition(partition_name, self.partition_deps)
        if partition_name == self.vivado_ip_partition_name():
            # The wrapper is a unit of the rack's own library, so it stands on
            # the rack partition and adds what packing the records needs.
            return self.__partition(
                partition_name,
                self.vivado_ip_deps | {self.partition_name()})
        return None

    def __partition(self, partition_name, deps):
        return Partition(
            name=partition_name,
            sources=[SourceFile(path=self.description_path,
                                file_type=self.FILE_TYPE)],
            deps=set(deps))


class GatecapDescriptionLoader(RepositoryLoader):
    """Reads one description file into a :class:`GatecapDescriptionRepository`.

    The description is not registered as a definition file: it is a source of
    the build, and a build definition and a source are two typologies of the
    same path. Its changes are tracked as any source's are, by the generation
    task that takes it as input.
    """

    LOADER_NAME = "gatecap-description"

    def load(self):
        rack = self.rack()
        return GatecapDescriptionRepository(
            name=rack.description.name.dotted(),
            path=Path(self.path),
            package=rack.package_name(),
            entity=rack.entity_name(),
            deps=rack.deps(),
            vivado_ip_deps=self.vivado_ip_deps(rack))

    @staticmethod
    def vivado_ip_deps(rack):
        """What the wrapper partition adds, or nothing when this rack cannot
        be wrapped at all. A rack whose boundary has no Vivado binding is a
        build error only for a project that asks for the wrapper, so the
        refusal is left for the partition to be named."""
        from acrobe_plugin.gatecap.generator import DescriptionError

        try:
            return rack.vivado_deps()
        except DescriptionError:
            return ()

    def rack(self):
        """Parse the description into a rack assembly, which knows the
        package the units land in and the partitions they need."""
        try:
            from acrobe_plugin.gatecap.generator import (DescriptionError,
                                                         DescriptionParser,
                                                         Generator)
        except ImportError as e:
            raise LoadError(
                f"{self.path}: a gatecap description needs the gatecap host "
                f"package: {e}") from e

        path = Path(self.path)
        if not path.is_file():
            raise LoadError(f"{path}: no such gatecap description")

        try:
            return Generator.of(DescriptionParser.load_file(path))
        except DescriptionError as e:
            raise LoadError(f"{path}: {e}") from e


__all__ = ["GatecapDescriptionLoader", "GatecapDescriptionRepository"]
