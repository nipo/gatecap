"""`acrobe gatecap generate` command surface.

Run: python3.13 -m pytest host/tests/test_generator_cli.py
"""

import subprocess

DESCRIPTION = """
name: my_pkg.my_capture
communication:
  mode: axi4_stream
  clock: la.control
instruments:
  la: !logic-analyzer
    domains:
      control:
        clock: clock
        frequency: 25_000_000
        signals:
          command: !axi4-stream
            trigger: vlr
"""


def generate(description, output):
    return subprocess.run(
        ["acrobe", "gatecap", "generate", str(description), "-o", str(output)],
        capture_output=True, text=True, timeout=60)


def test_generate_needs_no_capture_root(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text(DESCRIPTION)
    run = generate(path, tmp_path / "out")
    # No -r was given and none was demanded: the generator reads a file.
    assert "resource path is required" not in run.stderr
    assert "rack my_pkg.my_capture, 1 instrument(s) over axi4_stream" \
        in run.stderr
    assert run.returncode == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "my_capture.vhd", "my_capture_backplane.vhd", "my_capture_la.vhd",
        "my_pkg.gbs.yaml", "my_pkg.pkg.vhd"]
    for name in ("my_capture_la.vhd", "my_pkg.pkg.vhd",
                 "my_capture_backplane.vhd", "my_capture.vhd",
                 "my_pkg.gbs.yaml"):
        assert f"wrote {tmp_path / 'out' / name}" in run.stderr


def test_generate_rewrites_an_existing_directory(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text(DESCRIPTION)
    generate(path, tmp_path / "out")
    before = (tmp_path / "out" / "my_capture.vhd").read_text()
    assert generate(path, tmp_path / "out").returncode == 0
    assert (tmp_path / "out" / "my_capture.vhd").read_text() == before


def test_generate_reports_description_errors(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text("name: my_pkg.my_capture\n"
                    "communication:\n  mode: apb\n"
                    "instruments:\n  la: !logic-analyzer\n"
                    "    domains:\n      d:\n        signals:\n"
                    "          a: !bus 5\n")
    run = generate(path, tmp_path / "out")
    assert run.returncode == 1
    assert ("instruments.la.domains.d.signals.a, line 9: "
            "!bus takes a mapping of keys") in run.stderr


def test_generate_rejects_a_missing_description(tmp_path):
    run = generate(tmp_path / "absent.yaml", tmp_path / "out")
    assert run.returncode == 2
    assert "does not exist" in run.stderr
