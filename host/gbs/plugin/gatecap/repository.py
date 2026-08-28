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

    def __init__(self, name, path, package, deps):
        super().__init__(name, path.parent)
        self.description_path = path
        self.package = package
        self.partition_deps = frozenset(deps)

    def partition_name(self):
        return f"{self.LIBRARY}.{self.package}"

    def file_types(self):
        """A description is the only source this repository has, and it has
        it whatever the build asks for: the planner needs a consumer for the
        type in every plan the repository takes part in."""
        return {self.FILE_TYPE}

    def partition_lookup(self, partition_name, filter_vars):
        if partition_name != self.partition_name():
            return None
        return Partition(
            name=partition_name,
            sources=[SourceFile(path=self.description_path,
                                file_type=self.FILE_TYPE)],
            deps=set(self.partition_deps))


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
            deps=rack.deps())

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
