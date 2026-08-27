Overview
========

What it is
----------

In a FPGA design, you may want to capture and control some data to do
in-system debugging.  This can involve capturing in-design waveforms,
performing arbitrary bus accesses, inspecting clocks, and connecting a
tailor-made control panel to some wires.  Gatecap offers all this.

Gatecap is made of three main blocks:

* **Gateware** — a portable VHDL library with transport adapters,
  generic interconnection module, and instruments.

* **Generator** — from a description of what instruments you need,
  what signals they connect to and how they are clocked, framework
  generates the boilerplate VHDL code to instantiate the gateware
  blocks.

* **Host** — a Python driver, exposed as a plugin of `acrobe
  <https://github.com/nipo/acrobe>`_, with command-line and graphical
  interfaces.

Nothing about a given capture is configured twice. The core carries a
description of itself. Host queries it and spawn GUI from it. There is
no problem in loading an old bitstream, description never gets out of
sync.

What a core holds
-----------------

A gatecap core is a **rack**: one link to the host, one
self-description, and one or more *instruments* sitting in it.
Gatecap comes with builtin instruments.  Current builtins are:

The **logic analyzer**, it is the capture instrument.  Probes are
sampled on their own clock domain on a trigger.  Trigger can happen on
value or edge matching.  Trace dump can happen in multiple clock
domains simultaneously on the same trigger.

The **control/status panel**, it is a front panel of plain wires. It
is meant for manual interaction with control and status signals.

The **clock-rate measurer**, it answers a question a waveform cannot:
what do the clocks of this board actually run at? It uses a reference
clock to measure others.  The GUI can graph backlog of rate over time.

The **bus explorer**, it drives a register map through APB (APB itself
can be bridged to other bus fabrics).  This allows to fiddle with any
type of register map, like Dynamic Reconfiguration Ports or
memory-mapped IP blocks.

See :doc:`the catalogue <instrument/index>` for more details.  More
can be declared dynamically to gatecap as plugins
(:doc:`developer/extending`).

Design goals
------------

Portability
~~~~~~~~~~~

The gateware is plain VHDL-93 built on `NSL
<https://github.com/nipo/nsl/>`_, a vendor-neutral HDL library. It
carries no vendor primitive and no vendor-specific IP. It is built and
simulated with open tools. The same source targets Xilinx, Altera,
Gowin and Lattice devices.

Interchangeable communication channels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The core talks to the host over whatever link you already have, not
necessarily over a vendor's debug chain. The core description file
selects one among:

* **A frame interface of your own** — a pair of AXI4-Stream ports the
  design feeds from any transport that carries frames both ways. User
  can bridge this to any other transport.

* **JTAG** — through the FPGA's own test-access port, over a user
  custom data register. No pin, no specific transport: the capture
  shares the programming cable.

* **A UART** — two wires, with flow control and integrity.

* **SPI** — four wires, fast transport. Any acrobe-supported SPI bus
  adapter will be able to communicate with it.

* **SWD** — two wires, fast and reliable transport. Any
  acrobe-supported debug probe will be able to communicate with it.

* **A register bus** — no front end at all, just an APB completer for
  a requester already in your design. Can typically bridge efficiently
  on SoC-FPGA architectures.

* **USB** — with only 3 IOs (D+, D-, D+ pull) and an external
  resistor, the rack core can appear as a USB Full Speed device.

They are interchangeable in the strong sense: the link is a boundary
the rest of gatecap does not see through. Internally, gatecap basic
blocks are all interconnected around an APB bus fabric.  All the
transports above end up on that APB.  The same rack can be retargeted
on another transport for a minimal effort, this is transparent to the
host and the user.

See :doc:`communication/index` for the gateware side of each channel, and
:doc:`host/transports` for the path that reaches it.

Autodiscovery
~~~~~~~~~~~~~

On connection the host asks the core what it is. It gets back a
self-description that describes every instrument, names every probe,
gives the bus grouping and any enumerated values, states the buffer
geometry, the sampling clock rate and which trigger flavour is fitted.
UI is built from this.

A capture instance also exposes a *fingerprint*, a short value derived
from that description. The host watches it: if FPGA changes to a
differently-configured core while the UI is open, the mismatch is
noticed and the interface is recreated rather than silently showing
you a trace with the wrong names on it.

One driver, multiple interfaces
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The CLI, the GUI and API interaction are front-ends over the same
driver stack.  A capture run from a script and one run from the window
go through the same code, and produce the same trace. Use the CLI or a
script for regressions, automation and remote sessions; use the GUI to
explore. See :doc:`host/cli` and :doc:`host/gui`.
