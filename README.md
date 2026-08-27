# Gatecap

Gatecap is a portable, vendor-neutral tool suite for in-system
instrumentation of FPGA designs: capture in-design waveforms, drive
and observe plain wires, measure clocks, and perform arbitrary bus
accesses, all from a host computer over a single link.

A gatecap core is a **rack**: one transport link to the host (stream,
SPI, USB Full Speed, or debug ports like JTAG/SWD), one
self-description, and the instruments sitting in it. The core is
generated from a short YAML description of the instruments, their
signals and their clocking; the host queries the core's own
description, so nothing is configured twice and a bitstream is self
descriptive.

Gatecap host-side is a plugin to acrobe (see below). Acrobe provides
all the communication framework. Any *datagram* or *memory* interface
handled by acrobe can be used as gatecap transport.

## Interface

Gatecap can be used either through the CLI, including doing headless
captures; or using a GUI.  GUI can be used integrated into gatecap, or
exposed over a local HTTP server.

![Gatecap GUI](doc/images/gatecap-gui.png)

## Instruments

* **Logic analyzer** — trigger on value or edge matching, capture
  probes optionally in several domains using the same trigger.

* **Control/status panel** — a front panel of plain wires for manual
  interaction with control and status signals.

* **Clock-rate measurer** — measures the actual rate of clocks against
  a reference, with rate-over-time graphing in the GUI.

* **Bus explorer** — masters an APB port into the user design to read,
  write and poll any register map, with host-side SVD decode.

More instruments can be added as plugins.

## Built on

* [NSL](https://github.com/nipo/nsl) — the vendor-neutral VHDL
  component library the gateware depends on.
* [GBS](https://github.com/nipo/gbs) — the build system used for the
  gateware benches and examples.
* [acrobe](https://github.com/nipo/acrobe) — the asyncio hardware
  interfacing framework the host side plugs into, providing the
  command-line and graphical interfaces.
* [Surfer](https://surfer-project.org) for waveform rendering

## Documentation

The user manual is a sphinx tree in `doc/` (`make -C doc html`):
overview, usage walk-through, gateware integration and host-side CLI
and GUI. Design records live alongside it as `doc/*.md`
(`architecture.md`, `rack.md`, `generator.md`, per-instrument and
per-transport documents).

## License

MIT
