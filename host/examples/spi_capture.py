"""Capture a live SPI transaction with gatecap, dump it, and decode it back
with sigrok.

Two UDP bridges of the `spi_example` simulation are driven at once:

  * the gatecap capture core (udp 4242) -- armed on CS asserting low,
  * the NSL SPI transactor (udp 4243) -- runs real SPI transactions
    against the simulated SPI memory target.

The capture core probes the SPI bus wires and both the command and
response streams of the transactor. The demo seeds a memory word with a
write, then captures a read-back: the captured transaction carries the
read command on MOSI and the returned data on MISO. The window is written
as VCD; with --decode, sigrok-cli's SPI decoder turns it back into the
bytes that crossed the wire.

Usage:
    acrobe run spi_capture.py --output trace.vcd --decode
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.protocol import spi
from acrobe.root import root
from acrobe.component.nsl.transactor.spi import SpiTransactor

from acrobe_plugin.gatecap.instrument.la.blocks.control import Control
from acrobe_plugin.gatecap.cli import _write_vcd, _probe_names


class SpiFramedAdapter(Batcher, Node):
    """Drive a SpiTransactor codec over a raw framed datagram: encode the
    batch to one command frame, send it, await one response frame, decode.
    (Same shape as acrobe's private JTAG-SPI adapter, over UDP here.)"""

    def __init__(self, codec, channel, name="spi-xact"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.codec = codec
        self.channel = channel

    async def flush_ops(self, batch):
        cmd, _rsp_size, gather = self.codec.encode(batch)
        self.channel.send(cmd)
        rsp, _ = await self.channel.recv()
        self.codec.decode(batch, rsp, gather)


class SpiCaptureDemo:
    # Matches the spi_memory_controller in the testbench: opcode 0x0b
    # writes, any other opcode reads. Frames are opcode + addr + data.
    WRITE_OPCODE = 0x0B
    READ_OPCODE = 0x03
    ADDR_BYTES = 2
    DATA_BYTES = 2
    # The target streams read data immediately after the address bytes.
    READ_TURNAROUND = 0

    def __init__(self, capture_res, spi_port, base_freq, sck_freq):
        self.capture_res = capture_res
        self.spi_port = spi_port
        self.base_freq = base_freq
        self.sck_freq = sck_freq

    async def spi_target(self):
        datagram = await root(f"udp/127.0.0.1:{self.spi_port}")
        codec = SpiTransactor(self.base_freq)
        codec.freq_update(self.sck_freq)
        interface = spi.Interface(SpiFramedAdapter(codec, datagram), name="spi")
        target = spi.Target(interface, cs=0, mode=0, name="cs0")
        interface.child_add(target)
        return target

    async def control(self):
        node = await root(self.capture_res)
        controls = node.children_of_class(Control)
        if not controls:
            raise SystemExit("no gatecap control core found")
        return controls[0]

    @classmethod
    def write_frame(cls, addr, data):
        return bytes([cls.WRITE_OPCODE]
                     + list(addr.to_bytes(cls.ADDR_BYTES, "big"))
                     + list(data.to_bytes(cls.DATA_BYTES, "big")))

    @classmethod
    def read_frame(cls, addr):
        # opcode + addr, then turnaround + data idle bytes to clock the
        # data out on MISO.
        return bytes([cls.READ_OPCODE]
                     + list(addr.to_bytes(cls.ADDR_BYTES, "big"))
                     + [0] * (cls.READ_TURNAROUND + cls.DATA_BYTES))

    def frame_of(self, w, addr, data, read):
        a, d = addr + w, (data + w) & 0xFFFF
        return self.read_frame(a) if read else self.write_frame(a, d)

    async def run(self, count, pretrigger, addr, data, read, windows):
        control = await self.control()
        target = await self.spi_target()
        buffer = control.sink_node_get()

        if windows > control.max_windows:
            raise SystemExit(f"{windows} windows exceeds max {control.max_windows}")
        if windows * count > buffer.depth:
            raise SystemExit(f"{windows} x {count} samples exceeds buffer depth "
                             f"{buffer.depth}")

        if read:
            # Seed each address a window will read back (not captured).
            for w in range(windows):
                await target.transaction(spi.Shift(
                    self.write_frame(addr + w, (data + w) & 0xFFFF),
                    read_miso=False))

        # Each transaction's CS-low is one window's trigger; the core
        # re-arms itself between windows and stores a head per window. The
        # trigger is a separate block with its own probe names.
        trigger = control.trigger_node_get()
        cs_n_bit = _probe_names(trigger).index("cs_n")
        await trigger.configure(value=0, mask=1 << cs_n_bit)
        await control.configure(length=count, pre_trigger_len=pretrigger,
                                window_count=windows)
        await control.arm()

        frames = []
        for w in range(windows):
            frame = self.frame_of(w, addr, data, read)
            await target.transaction(spi.Shift(frame, read_miso=read))
            frames.append(frame)

        triggered, done = await control.wait_done()
        if done < windows:
            raise SystemExit(f"only {done} of {windows} windows captured")
        heads = await control.heads(windows)
        wins = [await buffer.read_window(h, count, control.signal_count)
                for h in heads]
        return control, wins, frames


def sigrok_decode(vcd_path):
    """Decode the VCD's SPI with sigrok-cli; return (mosi, miso) byte
    strings, or None if sigrok-cli is unavailable."""
    sigrok = shutil.which("sigrok-cli")
    if not sigrok:
        return None
    env = dict(os.environ)
    # This sigrok-cli is a local build linked against Homebrew libusb.
    env.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib")

    def decode(annotation):
        proc = subprocess.run(
            [sigrok, "-i", vcd_path, "-I", "vcd",
             "-P", "spi:clk=sck:mosi=mosi:miso=miso:cs=cs_n:"
                   "cs_polarity=active-low",
             "-A", f"spi={annotation}"],
            capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"sigrok-cli failed:\n{proc.stderr}")
        out = bytearray()
        for line in proc.stdout.splitlines():
            token = line.rsplit(":", 1)[-1].strip()
            if len(token) == 2 and all(c in "0123456789abcdefABCDEF" for c in token):
                out.append(int(token, 16))
        return bytes(out)

    return decode("mosi-data"), decode("miso-data")


async def main_async(args):
    demo = SpiCaptureDemo(args.capture, args.spi_port, args.base_freq, args.freq)
    control, wins, frames = await demo.run(args.count, args.pretrigger,
                                           args.addr, args.data, args.read,
                                           args.windows)
    names = _probe_names(control)
    # Lay the windows back-to-back so one VCD holds every transaction.
    samples = [s for win in wins for s in win]
    # Scalar vars (buses=False): this file is fed to sigrok, whose VCD import
    # drops vector vars and mis-parses the file if any is present. The dotted
    # hierarchy from the grouped names is still emitted as scopes.
    _write_vcd(args.output, names, samples, 0, control.sample_rate, buses=False)
    kind = "read" if args.read else "write"
    print(f"captured {len(wins)} {kind} window(s), {len(samples)} samples "
          f"-> {args.output}", file=sys.stderr)
    for w, frame in enumerate(frames):
        print(f"  window {w} issued MOSI: {frame.hex()}", file=sys.stderr)

    if args.decode:
        decoded = sigrok_decode(args.output)
        if decoded is None:
            print("sigrok-cli not found; skipping decode", file=sys.stderr)
            return
        dec_mosi, dec_miso = decoded
        print(f"sigrok MOSI: {dec_mosi.hex()}", file=sys.stderr)
        print(f"sigrok MISO: {dec_miso.hex()}", file=sys.stderr)
        expected = b"".join(frames)
        if dec_mosi != expected:
            raise SystemExit("MISMATCH: decoded MOSI != issued commands")
        print(f"MATCH: {len(frames)} transaction(s) decoded from the capture",
              file=sys.stderr)
        if args.read:
            off = 1 + demo.ADDR_BYTES + demo.READ_TURNAROUND
            for w in range(args.windows):
                want = ((args.data + w) & 0xFFFF).to_bytes(demo.DATA_BYTES, "big")
                base = w * (1 + demo.ADDR_BYTES + demo.READ_TURNAROUND
                            + demo.DATA_BYTES)
                if dec_miso[base + off:base + off + demo.DATA_BYTES] != want:
                    raise SystemExit(f"MISMATCH: window {w} MISO != seeded value")
            print("MATCH: every window's MISO returned its seeded value",
                  file=sys.stderr)


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", default="udp/127.0.0.1:4242/gatecap")
    p.add_argument("--spi-port", type=int, default=4243)
    p.add_argument("--base-freq", type=float, default=100e6,
                   help="transactor clock frequency (Hz)")
    # This transactor splits the divisor into DIVH/DIVL and the acrobe codec
    # only programs DIVH, so the effective SCK is ~8x slower than requested;
    # 25 MHz lands ~5.5 MHz SCK (18 samples/bit at 100 MHz), clean to decode.
    p.add_argument("--freq", type=float, default=25e6, help="SCK frequency (Hz)")
    p.add_argument("--count", type=int, default=1536,
                   help="samples per window")
    p.add_argument("--windows", type=int, default=1,
                   help="capture this many transactions, one per window")
    p.add_argument("--pretrigger", type=int, default=64)
    p.add_argument("--addr", type=lambda s: int(s, 0), default=0x0002)
    p.add_argument("--data", type=lambda s: int(s, 0), default=0xCAFE)
    p.add_argument("--output", default="spi_trace.vcd")
    p.add_argument("--read", action="store_true",
                   help="capture a read-back instead of a write (MISO carries "
                        "the data, but a read overflows a 1024-sample buffer)")
    p.add_argument("--decode", action="store_true",
                   help="decode the dump with sigrok-cli and check it")
    args = p.parse_args()
    await main_async(args)
