"""Scheduling of rack generation.

The whole build graph of the generated racks is built by the dispatcher's
constructor; :meth:`GatecapGenerateDispatcher.process` has nothing left to
do. See the constructor for why it cannot be otherwise.
"""

from gbs.base import BaseDispatcher
from gbs.build.task import BuildError, ResourceTypology
from gbs.ui.messages import MessageSeverity

from .task import GatecapGenerateTask


class GatecapGenerateDispatcher(BaseDispatcher):
    """Turns every pending description into one generation task.

    A description is a source of the library its rack is emitted into, so
    each one yields that library's VHDL files, and nothing else: what those
    files depend on came in with the description's own partition.
    """

    NAME = "gatecap-generate"
    TOOL_NAME = "gatecap"
    DESCRIPTION_TYPE = "gatecap-description"
    OUTPUT_TYPE = "vhdl"
    OUTPUT_DIRECTORY = "gatecap_generated"

    def __init__(self, context):
        super().__init__(context, name=self.NAME, tool_name=self.TOOL_NAME)

        # Everything happens here, in the constructor, rather than in
        # process(). Dispatchers are constructed once the pending queue
        # holds the project's sources and before any of them runs, and the
        # generated files have to be in the queue by then: the VHDL
        # analysis dispatcher freezes a per-library signature of its input
        # files on its first pass and silently ignores files appearing in
        # an already-signed library afterwards.
        self.racks = {}

        for description in context.filter_pending(
                file_type=[self.DESCRIPTION_TYPE]):
            rack = self.rack_of(description)
            name = self.claim(description, rack)
            outputs = self.outputs_of(description, rack, name)
            GatecapGenerateTask(dispatcher=self, source=description, rack=rack,
                                outputs=outputs)
            self.order(rack, outputs)
            self.info(f"rack {name} generated from {description.path.name}")

    async def process(self):
        pass

    def get_clean_paths(self):
        return {self.directory()}

    def directory(self):
        return self.context.output_path / self.OUTPUT_DIRECTORY

    def claim(self, description, rack):
        """Register a rack under its dotted name, which is also the name of
        the directory it is emitted into."""
        name = rack.description.name.dotted()
        if name in self.racks:
            raise BuildError(
                f"{description.path}: rack {name!r} is already generated "
                f"from {self.racks[name].path}")
        self.racks[name] = description
        return name

    def rack_of(self, description):
        """Parse one description into a rack assembly."""
        try:
            from acrobe_plugin.gatecap.generator import (DescriptionError,
                                                         DescriptionParser,
                                                         Generator)
        except ImportError as e:
            raise BuildError(
                f"a gatecap description needs the gatecap host package: {e}"
            ) from e

        try:
            return Generator.of(DescriptionParser.load_file(description.path))
        except DescriptionError as e:
            self.emit_tool_message(severity=MessageSeverity.ERROR,
                                   message=e.format(),
                                   file_path=description.path,
                                   line=e.line)
            raise BuildError(f"{description.path}: {e}") from e

    def outputs_of(self, description, rack, name):
        """One intermediate resource per emitted file, in analysis order,
        in the library the description itself belongs to."""
        directory = self.directory() / name
        return [self.context.get_resource(path=directory / file_name,
                                          file_type=self.OUTPUT_TYPE,
                                          library=description.library,
                                          typology=ResourceTypology.INTERMEDIATE,
                                          generated_by=self.name)
                for file_name in rack.file_names()]

    def order(self, rack, outputs):
        """Wire the generated files onto the libraries they are analysed
        after.

        The description carries those edges, but it is consumed by the
        generation task and leaves the queue; the files replacing it have to
        carry them instead, or their library ranks ahead of the libraries it
        instantiates from.
        """
        libraries = {dep.split(".", 1)[0] for dep in rack.deps()}
        dependencies = set()
        for library in libraries:
            dependencies.update(self.context.filter_pending(
                file_type=self.OUTPUT_TYPE, library=library))

        for output in outputs:
            self.context.add_pending(output, source_dependencies=dependencies)
