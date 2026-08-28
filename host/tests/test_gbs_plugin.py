"""The gbs plugin turning a description into the library it describes.

Run: python3.13 -m pytest host/tests/test_gbs_plugin.py
"""

import asyncio
import os

import pytest

gbs = pytest.importorskip("gbs", reason="gbs is not installed")

from gbs.build.context import BuildContext
from gbs.build.task import BuildError, ResourceTypology
from gbs.repository.loader import LoadError
from gbs.plugin.gatecap import (GatecapBackend, GatecapGeneratePass,
                                GatecapPlugin, gbs_register)
from gbs.plugin.gatecap.dispatcher import GatecapGenerateDispatcher
from gbs.plugin.gatecap.repository import (GatecapDescriptionLoader,
                                           GatecapDescriptionRepository)

DESCRIPTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "descriptions")
LIBRARY = GatecapDescriptionRepository.LIBRARY


class Context(BuildContext):
    """A build context with no plan behind it."""

    def __init__(self, output_path):
        super().__init__()
        self.output_path = output_path


class Project:
    """A one-description project as dependency resolution leaves it: the
    description in the library of the rack it stands for, the partitions it
    depends on already queued, and one hand-written unit against them."""

    def __init__(self, directory, description, hand_written=("tb.vhd",),
                 libraries=("gatecap", "nsl_amba")):
        self.directory = directory
        self.context = Context(directory / "gbs-build")
        self.dependencies = [
            self.source(directory / "libraries" / library / "partition.vhd",
                        "vhdl", library)
            for library in libraries]
        self.description = self.source(directory / description,
                                       "gatecap-description", LIBRARY)
        self.hand_written = [self.source(directory / name, "vhdl", "work")
                             for name in hand_written]

    def source(self, path, file_type, library):
        resource = self.context.get_resource(
            path, file_type=file_type, library=library,
            typology=ResourceTypology.SOURCE)
        self.context.add_pending(resource)
        return resource

    def dispatch(self):
        return GatecapGenerateDispatcher(self.context)

    def queue(self):
        return list(self.context.iter_pending())


@pytest.fixture
def description(tmp_path):
    """The single-domain description, copied where a project would hold
    it, with one hand-written unit next to it."""
    with open(os.path.join(DESCRIPTIONS, "single_domain.yaml")) as f:
        (tmp_path / "description.yaml").write_text(f.read())
    (tmp_path / "tb.vhd").write_text("-- nothing analysed here\n")
    return tmp_path / "description.yaml"


@pytest.fixture
def project(description):
    # A BuildStep is an asyncio.Future and wants a loop to attach to.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield Project(description.parent, description.name)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class TestRegistration:

    def test_entry_point_returns_the_plugin(self):
        assert isinstance(gbs_register(), GatecapPlugin)

    def test_plugin_carries_metadata(self):
        plugin = GatecapPlugin()
        assert plugin.name == "gbs.plugin.gatecap"
        assert plugin.description
        assert plugin.version

    def test_plugin_enumerates_one_backend(self):
        backends = GatecapPlugin().enumerate_backends()
        assert len(backends) == 1
        assert isinstance(backends[0], GatecapBackend)

    def test_plugin_registers_the_description_loader(self):
        parsers = GatecapPlugin().enumerate_repository_parsers()
        assert parsers == {"gatecap-description": GatecapDescriptionLoader}


class TestPass:

    def test_pass_consumes_descriptions_and_emits_vhdl(self):
        assert GatecapGeneratePass.input_types == {"gatecap-description"}
        assert GatecapGeneratePass.output_types == {"vhdl"}

    def test_a_description_is_classified_by_library(self):
        # The generated units land in the library of the description, so
        # the description has to carry one.
        assert "gatecap-description" in GatecapGeneratePass.types_with_library

    def test_backend_contributes_when_vhdl_is_wanted(self):
        passes = GatecapBackend().contribute_passes({}, {"vhdl", "simulation"})
        assert len(passes) == 1
        assert passes[0].name == GatecapGeneratePass.name

    def test_backend_stays_out_when_vhdl_is_not(self):
        assert GatecapBackend().contribute_passes({}, {"bitstream"}) == []


class TestRepository:

    def repository(self, description):
        return GatecapDescriptionLoader(description).load()

    def rack(self, description):
        from acrobe_plugin.gatecap.generator import (DescriptionParser,
                                                     Generator)
        return Generator.of(DescriptionParser.load_file(description))

    def test_the_partition_is_the_rack_package(self, description):
        repository = self.repository(description)
        assert repository.partition_name() == f"{LIBRARY}.probe_pkg"

    def test_the_partition_holds_the_description(self, description):
        partition = self.repository(description).partition_lookup(
            f"{LIBRARY}.probe_pkg", {})
        assert [(s.path, s.file_type) for s in partition.sources] == [
            (description, "gatecap-description")]

    def test_the_partition_depends_on_what_the_rack_needs(self, description):
        partition = self.repository(description).partition_lookup(
            f"{LIBRARY}.probe_pkg", {})
        assert partition.deps == set(self.rack(description).deps())
        assert "gatecap.capture" in partition.deps
        assert "nsl_amba.apb" in partition.deps

    def test_another_partition_is_not_ours(self, description):
        repository = self.repository(description)
        assert repository.partition_lookup(f"{LIBRARY}.other_pkg", {}) is None
        assert repository.partition_lookup("work.top", {}) is None

    def test_the_description_type_is_always_offered(self, description):
        # The planner requires a consumer for every offered type, and this
        # repository has exactly one source whatever the build asks for.
        assert self.repository(description).file_types() == {
            "gatecap-description"}

    def test_the_description_is_not_a_definition_file(self, description):
        # It is a source of the build; the two typologies would collide on
        # the same path.
        assert self.repository(description).definition_files == []

    def test_a_missing_description_names_itself(self, tmp_path):
        with pytest.raises(LoadError) as raised:
            GatecapDescriptionLoader(tmp_path / "nowhere.yaml").load()
        assert "nowhere.yaml" in str(raised.value)

    def test_a_broken_description_names_itself(self, tmp_path):
        path = tmp_path / "description.yaml"
        path.write_text("name: no_instruments_here\n")
        with pytest.raises(LoadError) as raised:
            GatecapDescriptionLoader(path).load()
        assert str(path) in str(raised.value)
        assert "instrument" in str(raised.value)


class TestDispatcher:

    def test_nothing_pending_schedules_nothing(self, tmp_path):
        context = Context(tmp_path / "gbs-build")
        assert GatecapGenerateDispatcher(context).racks == {}

    def test_the_rack_is_claimed_under_its_dotted_name(self, project):
        dispatcher = project.dispatch()
        assert set(dispatcher.racks) == {"probe_pkg.probe_capture"}

    def test_one_output_per_emitted_file(self, project):
        dispatcher = project.dispatch()
        generated = project.context.filter_pending(
            file_type="vhdl", generated_by=dispatcher.name)
        assert [r.path.name for r in generated] == [
            "probe_capture_la.vhd", "probe_pkg.pkg.vhd",
            "probe_capture_backplane.vhd", "probe_capture.vhd"]
        for resource in generated:
            assert resource.library == LIBRARY
            assert resource.typology == ResourceTypology.INTERMEDIATE
            assert resource.path.parent == (dispatcher.directory()
                                            / "probe_pkg.probe_capture")

    def test_the_description_is_consumed(self, project):
        project.dispatch()
        assert project.description not in project.queue()

    def test_generated_code_depends_on_the_libraries_it_uses(self, project):
        dispatcher = project.dispatch()
        generated = set(project.context.filter_pending(
            file_type="vhdl", generated_by=dispatcher.name))
        for dependency in project.dependencies:
            assert generated <= project.context.get_pending_dependents(
                dependency.path)

    def test_two_racks_of_one_name_are_a_build_error(self, project):
        twin = project.directory / "twin.yaml"
        twin.write_text(project.description.path.read_text())
        project.source(twin, "gatecap-description", LIBRARY)
        with pytest.raises(BuildError):
            project.dispatch()

    def test_a_broken_description_is_a_build_error(self, tmp_path):
        (tmp_path / "description.yaml").write_text("name: no_instruments_here\n")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            project = Project(tmp_path, "description.yaml", hand_written=())
            with pytest.raises(BuildError):
                project.dispatch()
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class TestTask:

    def test_the_declared_files_are_written(self, project):
        dispatcher = project.dispatch()
        task, = [step for step in project.context.steps
                 if step.name.startswith("gatecap_generate_")]
        asyncio.get_event_loop().run_until_complete(task.work())

        directory = dispatcher.directory() / "probe_pkg.probe_capture"
        written = sorted(os.listdir(directory))
        assert written == sorted(task.rack.file_names())

        rendered = task.rack.files()
        for name in written:
            assert (directory / name).read_text() == rendered[name]

    def test_the_partition_manifest_is_not_written(self, project):
        dispatcher = project.dispatch()
        task, = [step for step in project.context.steps
                 if step.name.startswith("gatecap_generate_")]
        asyncio.get_event_loop().run_until_complete(task.work())

        # The rack also renders a manifest for a stand-alone generated
        # directory; the build already knows what it says.
        assert "probe_pkg.gbs.yaml" in task.rack.files()
        directory = dispatcher.directory() / "probe_pkg.probe_capture"
        assert not os.path.exists(directory / "probe_pkg.gbs.yaml")
