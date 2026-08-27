"""Capture several SPI transactions in one run-length-encoded window.

Sibling of spi_capture.py, but the capture core runs in RLE mode: the idle
between transactions collapses to about one buffer line, so a whole burst of
transactions -- with their real inter-transaction gaps -- fits in a single
window that a raw capture could never hold. The decode is written to VCD as
changes (idle stays compact), and sigrok decodes every transaction.

Uses the spi_example_rle bench (capture on udp 4245, SPI on udp 4246).

Usage:
    acrobe run spi_capture_rle.py --windows 4 --output rle.vcd --decode
"""

from __future__ import annotations

import argparse
import sys

from vcd import VCDWriter

from acrobe.protocol import spi
from acrobe.root import root

from spi_capture import SpiCaptureDemo, sigrok_decode
from acrobe_plugin.gatecap.instrument.la.blocks.control import RleControl
from acrobe_plugin.gatecap.cli import _probe_names
from acrobe_plugin.gatecap.instrument.la.signals import VcdLayout


class RleSpiDemo(SpiCaptureDemo):
    async def control(self):
        node = await root(self.capture_res)
        controls = node.children_of_class(RleControl)
        if not controls:
            raise SystemExit("no RLE control core found")
        return controls[0]

    async def run(self, windows, addr, data, pre_lines, read):
        control = await self.control()
        target = await self.spi_target()

        if read:
            for w in range(windows):
                await target.transaction(spi.Shift(
                    self.write_frame(addr + w, (data + w) & 0xFFFF),
                    read_miso=False))

        # Trigger on the first CS-low, then RLE-encode everything after it:
        # the transactions and the (compressed) idle gaps between them. The
        # trigger is a separate block with its own probe names.
        trigger = control.trigger_node_get()
        cs_n_bit = _probe_names(trigger).index("cs_n")
        await trigger.configure(value=0, mask=1 << cs_n_bit)
        await control.configure(pre_lines=pre_lines)
        await control.arm()

        frames = []
        for w in range(windows):
            frame = self.frame_of(w, addr, data, read)
            await target.transaction(spi.Shift(frame, read_miso=read))
            frames.append(frame)

        await control.abort()
        await control.wait_done()
        runs, trig_run = await control.read_runs()
        return control, runs, trig_run, frames


def write_vcd_runs(output, names, runs, trig_run, sample_rate):
    # Change-based VCD straight from the RLE runs: time advances by each
    # run's dwell, so idle costs nothing. Time 0 is the capture start; the
    # trigger time is recorded in the comment (VCD time cannot be negative).
    # Scalar vars (buses=False): this file is fed to sigrok, whose VCD import
    # drops vector vars and mis-parses the file if any is present.
    if sample_rate:
        period_ps = max(1, round(1_000_000_000_000 / sample_rate))
        timescale = "1 ps"
    else:
        period_ps = 1
        timescale = "1 ns"
    trig_t = sum(c for _, c in runs[:trig_run]) * period_ps
    layout = VcdLayout(names, buses=False)
    stream = open(output, "w") if output else sys.stdout
    try:
        with VCDWriter(stream, timescale=timescale, date="",
                       comment=f"gatecap RLE capture, trigger at {trig_t} "
                               f"{timescale}") as writer:
            layout.register(writer)
            t = 0
            for value, count in runs:
                layout.emit(writer, t * period_ps, value)
                t += count
    finally:
        if output:
            stream.close()


async def main_async(args):
    demo = RleSpiDemo(args.capture, args.spi_port, args.base_freq, args.freq)
    control, runs, trig_run, frames = await demo.run(args.windows, args.addr,
                                                     args.data, args.pre_lines,
                                                     args.read)
    names = _probe_names(control)
    total = sum(c for _, c in runs)
    write_vcd_runs(args.output, names, runs, trig_run, control.sample_rate)
    kind = "read" if args.read else "write"
    print(f"captured {args.windows} {kind} transaction(s): {len(runs)} RLE runs "
          f"= {total} samples -> {args.output}", file=sys.stderr)
    print(f"trigger at run {trig_run}", file=sys.stderr)
    for w, frame in enumerate(frames):
        print(f"  txn {w} MOSI: {frame.hex()}", file=sys.stderr)

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
        print(f"MATCH: {len(frames)} transaction(s) decoded from one RLE window",
              file=sys.stderr)


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", default="udp/127.0.0.1:4245/gatecap")
    p.add_argument("--spi-port", type=int, default=4246)
    p.add_argument("--base-freq", type=float, default=100e6)
    p.add_argument("--freq", type=float, default=25e6, help="SCK frequency (Hz)")
    p.add_argument("--windows", type=int, default=4,
                   help="SPI transactions to capture in the one window")
    p.add_argument("--pre-lines", type=int, default=16,
                   help="RLE pre-trigger ring size in lines")
    p.add_argument("--addr", type=lambda s: int(s, 0), default=0x0002)
    p.add_argument("--data", type=lambda s: int(s, 0), default=0xCAFE)
    p.add_argument("--output", default="spi_rle_trace.vcd")
    p.add_argument("--read", action="store_true",
                   help="read-back transactions (MISO carries data)")
    p.add_argument("--decode", action="store_true",
                   help="decode the dump with sigrok-cli and check it")
    args = p.parse_args()
    await main_async(args)
