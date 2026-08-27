"""gbs partition manifest emission.

Run: python3.13 -m pytest host/tests/test_generator_gbs.py
"""

import pytest
import yaml

from acrobe_plugin.gatecap.generator import DescriptionParser, GbsManifest


def test_manifest_rendering():
    manifest = GbsManifest.of(["my_pkg.pkg.vhd", "my_capture.vhd"],
                              ["gatecap.control", "nsl_amba.apb",
                               "gatecap.control"])
    assert manifest.render() == """\
sources:
  - file_type: vhdl
    files:
      - my_pkg.pkg.vhd
      - my_capture.vhd
deps:
  - gatecap.control
  - nsl_amba.apb
"""


def test_manifest_has_exactly_two_keys():
    document = yaml.safe_load(
        GbsManifest.of(["e.vhd"], ["gatecap.trace"]).render())
    assert set(document) == {"sources", "deps"}
    assert document["sources"] == [{"file_type": "vhdl", "files": ["e.vhd"]}]
    assert document["deps"] == ["gatecap.trace"]


def test_manifest_without_dependencies_stays_a_list():
    document = yaml.safe_load(GbsManifest.of(["e.vhd"], []).render())
    assert document["deps"] == []


def test_manifest_is_deterministic_and_source_ordered():
    first = GbsManifest.of(["a.vhd", "b.vhd"], ["z.p", "a.p"])
    second = GbsManifest.of(["a.vhd", "b.vhd"], ["a.p", "z.p", "a.p"])
    assert first.render() == second.render()
    assert first.sources == ("a.vhd", "b.vhd")
    assert first.deps == ("a.p", "z.p")


def test_manifest_from_a_description():
    description = DescriptionParser.load("""
name: my_pkg.my_capture
communication:
  mode: axi4_stream
  clock: la.ctrl
instruments:
  la: !logic-analyzer
    domains:
      ctrl:
        signals:
          command: !axi4-stream
            trigger: vlr
""")
    manifest = GbsManifest.of(
        [f"{description.name.package}.pkg.vhd",
         f"{description.name.entity}.vhd"], description.deps())
    document = yaml.safe_load(manifest.render())
    assert document["sources"] == [
        {"file_type": "vhdl", "files": ["my_pkg.pkg.vhd", "my_capture.vhd"]}]
    for dep in ("gatecap.axi4_stream_packer", "nsl_amba.axi4_stream",
                "gatecap.capture", "gatecap.control", "gatecap.descriptor"):
        assert dep in document["deps"]
    assert document["deps"] == sorted(document["deps"])


def test_empty_partition_is_a_programming_error():
    with pytest.raises(AssertionError):
        GbsManifest.of([], ["gatecap.trace"]).render()


def test_manifest_write(tmp_path):
    path = tmp_path / "generated.gbs.yaml"
    GbsManifest.of(["e.vhd"], ["gatecap.trace"]).write(path)
    assert path.read_text() == GbsManifest.of(["e.vhd"],
                                              ["gatecap.trace"]).render()
