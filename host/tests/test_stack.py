"""End-to-end stack test: gateware -> UDP -> plugin -> acrobe CLI.

Builds and runs the socket simulator, then drives it through the real
`acrobe gatecap` commands and checks the captured trace. Guards the whole
chain in one shot.

Run: python3.13 -m pytest host/tests/
"""

import csv
import os
import subprocess
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Two sims run side by side: the plain trace buffer and the byte-lane
# packed one, on distinct UDP ports. Every capture test runs against both.
SIMS = [
    (os.path.join(REPO, "gateware", "example", "socket"), 4242),
    (os.path.join(REPO, "gateware", "example", "socket_packed"), 4243),
]
RESOURCE = "udp/127.0.0.1:4242/gatecap"
RESOURCE_PACKED = "udp/127.0.0.1:4243/gatecap"
RESOURCES = [RESOURCE, RESOURCE_PACKED]


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket"],
                   capture_output=True)


@pytest.fixture(scope="session")
def sim():
    _kill_stale()
    time.sleep(0.5)
    procs = []
    for sim_dir, _ in SIMS:
        build = subprocess.run(["gbs", "project", "build"], cwd=sim_dir,
                               capture_output=True, text=True)
        assert build.returncode == 0, build.stdout + build.stderr
        sim_bin = os.path.join(sim_dir, "tb")
        assert os.path.exists(sim_bin), "simulator executable missing after build"
        procs.append(subprocess.Popen([sim_bin, "--ieee-asserts=disable"],
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL))
    time.sleep(1.0)  # let the sims bind their UDP ports
    try:
        yield
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _kill_stale()


def _gatecap(*args, resource=RESOURCE, timeout=60):
    return subprocess.run(["acrobe", "gatecap", "-r", resource, *args],
                          capture_output=True, text=True, timeout=timeout)


def _capture(tmp_path, trigger, count, pretrigger=0, resource=RESOURCE):
    out = tmp_path / "trace.csv"
    args = ["capture", "control.control", "--trigger", trigger,
            "--count", str(count), "--output", str(out)]
    if pretrigger:
        args += ["--pretrigger", str(pretrigger)]
    proc = _gatecap(*args, resource=resource)
    assert proc.returncode == 0, proc.stderr
    rows = list(csv.reader(out.open()))
    header, data = rows[0], rows[1:]
    assert header[0] == "sample"
    assert len(header) == 9  # sample column + 8 probes
    values = []
    for i, row in enumerate(data):
        # The sample column is relative to the trigger sample.
        assert int(row[0]) == i - pretrigger
        values.append(sum(int(b) << k for k, b in enumerate(row[1:])))
    return values


def _read_vcd(path):
    from vcd.reader import TokenKind, tokenize
    bit_of = {}    # id_code -> bit index (registration order == bit order)
    bits = {}      # id_code -> current value
    by_time = {}   # time -> reconstructed sample value
    time = 0
    with open(path, "rb") as f:
        for tok in tokenize(f):
            if tok.kind is TokenKind.VAR:
                bit_of[tok.data.id_code] = len(bit_of)
            elif tok.kind is TokenKind.CHANGE_TIME:
                time = tok.data
            elif tok.kind is TokenKind.CHANGE_SCALAR:
                bits[tok.data.id_code] = int(tok.data.value)
            by_time[time] = sum(v << bit_of[c] for c, v in bits.items())
    return [(t, by_time[t]) for t in sorted(by_time)]


# The plain buffer stores one sample per 32-bit word; the packed buffer
# stores 8-bit samples one per byte lane. Both read back identically.
_STRIDE_BITS = {RESOURCE: 32, RESOURCE_PACKED: 8}


@pytest.mark.parametrize("resource", RESOURCES, ids=["plain", "packed"])
def test_info(sim, resource):
    proc = _gatecap("info", resource=resource)
    assert proc.returncode == 0, proc.stderr
    assert "control:" in proc.stdout
    assert "probes (8):" in proc.stdout
    # names emitted by the gateware descriptor
    assert "count0" in proc.stdout and "count7" in proc.stdout
    # trigger capabilities from the descriptor
    assert "value-mask match" in proc.stdout
    assert "pre-trigger capable" in proc.stdout
    assert "sample clock: 100 MHz" in proc.stdout
    # the packing shows up as the advertised sample stride and depth
    assert f"{_STRIDE_BITS[resource]}-bit samples" in proc.stdout
    assert "depth 64 samples" in proc.stdout


@pytest.mark.parametrize("resource", RESOURCES, ids=["plain", "packed"])
def test_capture_immediate(sim, tmp_path, resource):
    # Match-all trigger: capture the free-running counter, consecutive.
    values = _capture(tmp_path, "0/0", 16, resource=resource)
    assert len(values) == 16
    for i in range(1, len(values)):
        assert values[i] == (values[0] + i) & 0xFF, values


@pytest.mark.parametrize("resource", RESOURCES, ids=["plain", "packed"])
def test_capture_on_value(sim, tmp_path, resource):
    # Trigger on a specific counter value: first sample is that value.
    values = _capture(tmp_path, "0x80/0xff", 4, resource=resource)
    assert values == [0x80, 0x81, 0x82, 0x83], values


@pytest.mark.parametrize("resource", RESOURCES, ids=["plain", "packed"])
def test_capture_pretrigger(sim, tmp_path, resource):
    # Trigger on 0x80 with 3 pre-trigger samples: the window holds the
    # counter values around the trigger, with 0x80 at relative index 0.
    # On the packed buffer this exercises a head that is not word-aligned.
    values = _capture(tmp_path, "0x80/0xff", 8, pretrigger=3, resource=resource)
    assert values == [0x7d, 0x7e, 0x7f, 0x80, 0x81, 0x82, 0x83, 0x84], values


@pytest.mark.parametrize("resource", RESOURCES, ids=["plain", "packed"])
def test_capture_pretrigger_vcd(sim, tmp_path, resource):
    # VCD timeline runs from the oldest sample at t=0; the trigger sample
    # (0x80) lands at pretrigger*period = 30000 ps.
    out = tmp_path / "trace.vcd"
    proc = _gatecap("capture", "control.control", "--trigger", "0x80/0xff",
                    "--count", "8", "--pretrigger", "3", "--output", str(out),
                    resource=resource)
    assert proc.returncode == 0, proc.stderr
    tv = _read_vcd(out)
    times = [t for t, _ in tv]
    values = [v for _, v in tv]
    assert values == [0x7d, 0x7e, 0x7f, 0x80, 0x81, 0x82, 0x83, 0x84], values
    # 100 MHz -> 10000 ps/sample; trigger value 0x80 at t = 3 * 10000.
    assert times[0] == 0
    assert dict(tv)[30000] == 0x80
    assert all(times[i] - times[i - 1] == 10000 for i in range(1, len(times)))


@pytest.mark.parametrize("resource", RESOURCES, ids=["plain", "packed"])
def test_capture_vcd(sim, tmp_path, resource):
    # .vcd extension selects VCD; round-trip it back to sample values.
    out = tmp_path / "trace.vcd"
    proc = _gatecap("capture", "control.control", "--trigger", "0x80/0xff",
                    "--count", "4", "--output", str(out), resource=resource)
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    tv = _read_vcd(out)
    times = [t for t, _ in tv]
    values = [v for _, v in tv]
    assert values == [0x80, 0x81, 0x82, 0x83]
    # 100 MHz sim clock -> 10 ns = 10000 ps between samples
    assert all(times[i] - times[i - 1] == 10000 for i in range(1, len(times)))


def test_capture_bad_control(sim):
    proc = _gatecap("capture", "nope", "--count", "4")
    assert proc.returncode != 0
    assert "no capture target" in proc.stderr
