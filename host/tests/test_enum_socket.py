"""End-to-end enum test: a counter advertised as an enum bus, captured
through the real gateware -> UDP -> plugin -> acrobe CLI path.

The socket_enum harness names the 8 counter bits as one bus with an enum
that inherits a base (+demo.phase = 0..2), extends it with custom values
(3..5), and leaves 6..255 undefined. Capturing a full counter period lets
us check every decode path in the CSV and VCD output, plus a
trigger-by-label match.

Run: python3.13 -m pytest host/tests/test_enum_socket.py
"""

import csv
import os
import subprocess
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_enum")
RESOURCE = "udp/127.0.0.1:4247/gatecap"

# count -> label for a full period: 0..2 inherited, 3..5 custom, 6.. undefined.
EXPECTED_HEAD = ["IDLE", "START", "RUN", "custom0", "custom1", "STOP", "0x6", "0x7"]


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_enum"],
                   capture_output=True)


@pytest.fixture(scope="session")
def sim():
    _kill_stale()
    time.sleep(0.5)
    build = subprocess.run(["gbs", "project", "build"], cwd=SIM_DIR,
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    sim_bin = os.path.join(SIM_DIR, "tb")
    assert os.path.exists(sim_bin), "simulator executable missing after build"
    proc = subprocess.Popen([sim_bin, "--ieee-asserts=disable"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)  # let the sim bind its UDP port
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _kill_stale()


def _gatecap(*args, timeout=60):
    return subprocess.run(["acrobe", "gatecap", "-r", RESOURCE, *args],
                          capture_output=True, text=True, timeout=timeout)


def _capture(tmp_path, name, trigger, count):
    out = tmp_path / name
    proc = _gatecap("capture", "control.control", "--trigger", trigger,
                    "--count", str(count), "--output", str(out))
    assert proc.returncode == 0, proc.stderr
    return out


def test_enum_csv_decodes_every_path(sim, tmp_path):
    # Trigger by label (count=IDLE resolves to 0) and capture a full period.
    out = _capture(tmp_path, "enum.csv", "count=IDLE", 256)
    rows = list(csv.reader(out.open()))
    header, data = rows[0], rows[1:]
    assert header == ["sample", "count"]   # one bus column, not eight bits
    assert len(data) == 256

    col = [row[1] for row in data]
    # A full period contains count==0 exactly once; check the run after it.
    start = col.index("IDLE")
    seq = [col[(start + k) % 256] for k in range(len(EXPECTED_HEAD))]
    assert seq == EXPECTED_HEAD


def test_enum_vcd_uses_string_labels(sim, tmp_path):
    out = _capture(tmp_path, "enum.vcd", "count=IDLE", 256)
    text = out.read_text()
    assert "$var string" in text          # enum bus is a string var
    for label in ("IDLE", "RUN", "STOP", "custom0"):
        assert label in text
    assert "0x6" in text                   # undefined value falls back to hex


def test_enum_trigger_by_label_anchors(sim, tmp_path):
    # count=RUN resolves to 2, so the window lands around counts 2..5. Search
    # for the contiguous decode (robust to a one-cycle trigger skew).
    out = _capture(tmp_path, "run.csv", "count=RUN", 16)
    rows = list(csv.reader(out.open()))
    labels = [row[1] for row in rows[1:]]
    want = ["RUN", "custom0", "custom1", "STOP"]
    assert any(labels[i:i + len(want)] == want
               for i in range(len(labels) - len(want) + 1)), labels
