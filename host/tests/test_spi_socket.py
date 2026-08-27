"""End-to-end test of a rack reached over plain SPI, over the socket_spi
simulator.

The core is emitted by ``acrobe gatecap generate`` from
``gateware/example/socket_spi/description.yaml``: a logic analyzer and a
control/status panel behind the ``spi`` communication mode. The bench hangs the
rack's four SPI pins off an NSL framed SPI transactor, and the host drives that
transactor as its SPI master -- so everything below
``acrobe.protocol.spi.Target`` is the master's business and everything above it
is the transport under test.

What is checked here: discovery -- the SFDP-shaped 0x5a read whose blob states
where the descriptor is, then the descriptor there (the root type UUID and the
fingerprint),
registers written and read
back through single-word transactions, the status poll as one burst, a trace
readback chunked into several chip-select assertions at ``max_burst``, the
refusal of a partial-word write, and reads that end at a mapped region's last
word -- where the adapter's one-word read-ahead lands outside the burst -- still
reading what word-by-word reads do.

Run: python3.13 -m pytest host/tests/test_spi_socket.py
"""

import asyncio
import io
import os
import subprocess
import time

import cbor2
import pytest

from acrobe.adapter.model import reset_hw_root_for_tests

from acrobe_plugin.gatecap.instrument.control_status.driver import \
    ControlStatusPanel
from acrobe_plugin.gatecap.instrument.la.driver import LogicAnalyzer
from acrobe_plugin.gatecap.rack import RACK_UUID
from acrobe_plugin.gatecap.session import Session
from acrobe_plugin.gatecap.spi import SpiRack

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket_spi")
# The master is the bench's framed transactor: at 100 MHz in and 10 MHz asked
# for, its divisor puts SCK at exactly 10 MHz, the highest rate the rack was
# elaborated for.
RESOURCE = "udp/127.0.0.1:4254/nsl_spi(fin=100M,fmax=10M)/cs0/gatecap"

COUNT = 256
PRETRIGGER = 16


def _kill_stale():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket_spi"],
                   capture_output=True)


@pytest.fixture(scope="module")
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
    reset_hw_root_for_tests()
    try:
        yield
    finally:
        reset_hw_root_for_tests()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _kill_stale()


async def _session():
    session = Session(RESOURCE)
    await session.open()
    return session


def _rack_of(session):
    """The transport node under the enumerated blocks: the address space every
    driver reads through."""
    return session.fingerprinted()[0].bridge


class Transactions:
    """Counts chip-select assertions, by standing in front of the SPI target
    the transport was given. What one access costs on this link is exactly how
    many transactions it takes."""

    def __init__(self, rack):
        self.rack = rack
        self.target = rack.target
        self.count = 0

    def __enter__(self):
        self.rack.target = self
        return self

    def __exit__(self, *exc):
        self.rack.target = self.target
        return False

    def transaction(self, *shifts):
        self.count += 1
        return self.target.transaction(*shifts)


async def _settled(read, expected, tries=200):
    """The panel runs on a clock unrelated to the transport's, so a level it
    reports is asked for until it has crossed."""
    for _ in range(tries):
        value = await read()
        if value == expected:
            return value
    return value


def test_a_rack_is_discovered_over_spi_with_no_framing_at_all(sim):
    async def run():
        session = await _session()
        rack = _rack_of(session)
        # The transport is the SPI one, mapped in 32-bit words like every
        # other gatecap link.
        assert isinstance(rack, SpiRack)
        assert rack.word_bytes == 4
        assert rack.max_burst == SpiRack.MAX_BURST

        # The connection went through the discovery blob: the address width
        # is nowhere on the host side, so a value here came off the wire, and
        # so did the base the descriptor was then read at.
        assert rack.addr_bits == 24
        assert rack.base == 0
        with Transactions(rack) as counted:
            payload = await rack.discovery_blob()
        assert counted.count == 1
        assert payload[:4] == b"GCAP"
        # At the SFDP data position, past the filler the payload opens with:
        # "GCAP", a three-element CBOR array, and the read's trailing zeros.
        assert payload == b"GCAP" + bytes([0x83, 0x18, 0x18, 0x02, 0x00]) \
            + bytes(SpiRack.DISCOVERY_BYTES - 9)

        # Past the blob, nothing was needed to find the rack but the
        # descriptor at the base the blob gave.
        root = cbor2.load(io.BytesIO(await rack.mem_read(rack.base, 1024)))
        assert root[0] == RACK_UUID

        names = [block.name for block in session.blocks()]
        for name in ("la", "main.control", "main.buffer", "main.trigger",
                     "panel", "registers"):
            assert name in names, names
        assert len([b for b in session.blocks()
                    if isinstance(b, LogicAnalyzer)]) == 1
        assert len([b for b in session.blocks()
                    if isinstance(b, ControlStatusPanel)]) == 1

        # One instance, one fingerprint, whichever block reports it.
        assert session.fingerprint == await session.fingerprinted()[0] \
            .fingerprint()
        await session.close()

    asyncio.run(run())


def test_a_register_is_written_and_read_back_through_the_link(sim):
    async def run():
        session = await _session()
        panel = session.block_by_name("panel")

        await panel.control_write("led", 1)
        await panel.control_write("level", 0xABC)
        assert await panel.controls_read() == {"led": 1, "level": 0xABC}
        # And out of the instrument's ports, through the bench loopback.
        assert await _settled(lambda: panel.status_read("level_echo"),
                              0xABC) == 0xABC
        assert await panel.status_read("led_echo") == 1

        await panel.control_write("level", 0x123)
        assert await panel.control_read("level") == 0x123
        assert await _settled(lambda: panel.status_read("level_echo"),
                              0x123) == 0x123

        await session.close()

    asyncio.run(run())


def test_a_status_poll_is_one_chip_select_assertion(sim):
    async def run():
        session = await _session()
        control = session.block_by_name("main.control")
        rack = _rack_of(session)

        # STATUS and FINGERPRINT are adjacent words, so the poll is one burst
        # -- one opcode, one address, one turnaround for both of them.
        with Transactions(rack) as counted:
            poll = await control.poll_raw()
        assert counted.count == 1
        assert poll["fingerprint"] == session.fingerprint
        assert poll["state"] == control.STATE_IDLE

        await session.close()

    asyncio.run(run())


def test_a_long_read_is_cut_into_bursts_at_max_burst(sim):
    async def run():
        session = await _session()
        control = session.block_by_name("main.control")
        trigger = control.trigger_node_get()
        value, mask = trigger.ui_adaptor("console").parse_terms(["state=DONE"])
        await trigger.configure(value, mask)
        await control.configure_and_arm(count=COUNT, pretrigger=PRETRIGGER)
        triggered, _ = await control.wait_done(tries=4000)
        assert triggered

        # The same window, read as one burst per 64 words and as one burst per
        # 4: the wire carries no length, so what a burst holds is the master's
        # choice alone and the data cannot depend on it.
        wide = await control.read_trace(count=COUNT, pretrigger=PRETRIGGER)
        rack = _rack_of(session)
        rack.max_burst = 4
        try:
            with Transactions(rack) as counted:
                narrow = await control.read_trace(count=COUNT,
                                                  pretrigger=PRETRIGGER)
        finally:
            rack.max_burst = SpiRack.MAX_BURST
        assert narrow["windows"][0] == wide["windows"][0]
        assert len(wide["windows"][0]) == COUNT
        # One sample is one word here, so the trace alone is COUNT/4 bursts.
        assert counted.count >= COUNT // 4

        # The trigger sample is the matched one, whichever burst it arrived in.
        names = control.signal_names
        sample = wide["windows"][0][PRETRIGGER]
        state = sum(((sample >> names.index(f"state[{i}]")) & 1) << i
                    for i in range(2))
        assert state == 3      # DONE

        await session.close()

    asyncio.run(run())


def test_a_partial_word_write_is_refused(sim):
    async def run():
        session = await _session()
        panel = session.block_by_name("panel")
        rack = _rack_of(session)
        base = session.block_by_name("panel.registers").base

        await panel.control_write("level", 0x555)
        for addr, data in ((base + 0x300, b"\x01"),
                           (base + 0x300, b"\x01\x02\x03"),
                           (base + 0x302, b"\x01\x02\x03\x04")):
            with pytest.raises(ValueError, match="whole 4-byte words"):
                await rack.mem_write(addr, data)
        # And nothing of the refused writes reached the wire.
        assert await panel.control_read("level") == 0x555

        await session.close()

    asyncio.run(run())


def test_the_read_ahead_past_a_burst_disturbs_nothing(sim):
    async def run():
        session = await _session()
        rack = _rack_of(session)
        panel = session.block_by_name("panel")
        registers = session.block_by_name("panel.registers")
        instruments = {i.name: i for i in session.instruments()}
        top = max(i.base + i.size for i in instruments.values())

        # Every read burst reads one word past its end -- the adapter cannot
        # know which word is the last -- so a burst ending on the last word of
        # a mapped region reads outside it. That must change nothing: what a
        # burst ending there returns is what word-by-word reads return.
        for end in (top, instruments["panel"].base + instruments["panel"].size):
            burst = await rack.mem_read(end - 16, 16)
            words = bytearray()
            for offset in range(0, 16, 4):
                word = await rack.read32(end - 16 + offset)
                words += word.to_bytes(4, "little")
            assert burst == bytes(words)

        # A register read straight after one of those bursts is unaffected: a
        # read left in flight when the chip select dropped cannot leak into the
        # next transaction.
        await panel.control_write("level", 0x2A5)
        await rack.mem_read(top - 4, 4)
        assert await panel.control_read("level") == 0x2A5

        # A byte range is widened to the words covering it and clipped back,
        # so an unaligned read of the same registers agrees with the words.
        base = registers.base + 0x200
        words = await rack.mem_read(base, 8)
        assert await rack.mem_read(base + 1, 3) == words[1:4]
        assert await rack.mem_read(base + 3, 2) == words[3:5]

        await session.close()

    asyncio.run(run())
