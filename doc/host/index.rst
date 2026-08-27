Host side
=========

.. toctree::
   :maxdepth: 2

   install
   transports
   cli
   gui

Philosophy
----------

The host program knows nothing about your board and very little about your
capture core. It knows how to send a frame and receive one, and it knows how
to ask a gatecap instance to describe itself. Everything else — the signal
names, the buses, the trigger editor, the buffer geometry, the time axis —
comes from the core.

acrobe as the transport layer
-----------------------------

`acrobe <https://github.com/nipo/acrobe>`_ is a generic toolset for talking
to hardware over whatever link is available: JTAG probes, serial ports,
network sockets, USB devices, SPI and SWD. Two of its abstractions matter
here: ``Datagram``, a two-way channel carrying discrete framed messages, and
its memory model, a space of registers a node reads and writes.

gatecap's driver reads and writes registers through an *address space*, and
knows nothing else about the link. Any acrobe transport that can provide one
can carry a capture session — either by carrying frames, which the driver
turns into an address space of its own, or by being a memory already:

* **JTAG** — through a probe acrobe supports, on an NSL framed FIFO in the
  FPGA; the capture shares the programming cable.
* **UART** or any byte pipe — with HDLC framing on top.
* **USB** — a framed endpoint of a device function.
* **Network** — a UDP socket to the board (or to a simulator, see
  :doc:`../developer/simulation`).
* **SWD** — a debug port of the core's own, where the access port *is* the
  rack and its memory space the address space.
* **SPI** — the rack as a chip on the bus, addressed like a flash: opcode,
  address, data.

Because the driver stops at that boundary, moving a session from a simulation
socket to a JTAG cable to a USB link changes the resource path and nothing
else — not the trigger you set, not the trace you get.

Resource paths
--------------

A target is named by a path through acrobe's device tree: the adapter, its
address, the framing, and the handler at the end. The last component,
``gatecap``, is what says "there is a capture instance here"::

   udp/127.0.0.1:4242/gatecap

Resolving that path opens the transport, spawns the gatecap node on it, and
starts discovery.

The segments in between are what differs from one link to the next — a JTAG
chain, an SWD debug port, an HDLC framer over a serial line.
:doc:`transports` gives the path for each of them, and the one backend that
ends in something other than ``gatecap``.

A plugin, not a program
-----------------------

The gatecap host driver ships as an acrobe *plugin*. Installing it adds:

* the ``gatecap`` handler on acrobe's datagram layer, and a ``gatecap`` chip
  type on a SPI chip select, so a transport path can end in ``gatecap``
  whichever of the two the link is;
* the drivers the discovery step instantiates — the logic-analyzer instrument
  with the capture control, trigger and trace buffer blocks under it, the
  control/status panel with its register file, the clock-rate measurer, and
  the bus explorer with its engine register file;
* the ``acrobe gatecap`` command group (:doc:`cli`), including the GUI.

Living inside acrobe means a capture session shares the tool, the transports
and the device tree with everything else you do to that board: the same
session that programs a bitstream or drives a SPI transactor can hold a
capture.

Discovery
---------

On connection the driver reads the core's self-description and builds the tree
it describes, on two levels: one node per *instrument* the core holds — a
logic analyzer is one, a control/status panel another, a clock measurer a
third, a bus explorer a fourth — and under each of them
one node per register block it is built from, a capture control, its trigger,
its trace buffer. Both levels
are identified by a type identifier, and the driver looks up an implementation
for each; one it does not recognise still appears in the tree, it just has no
user interface, and an unknown instrument's blocks still get their own
drivers. That is what allows the gateware to grow new types without breaking
older hosts.

The description also yields a fingerprint. Both front-ends poll it along with
the capture status: if the FPGA is reprogrammed with a different
configuration while you are connected, the change is detected instead of
being papered over.

Scripting
---------

The same drivers are usable directly from Python when neither front-end fits
— a regression that arms a capture, drives a stimulus over a second
transport, and asserts on the result. A script defines an ``async def
main()`` and nothing else — no event loop, no lifecycle calls — and is run
with ``acrobe run script.py [args…]``: acrobe supplies the event loop,
logging, plugin loading and teardown around it, and hands the arguments
through as ``sys.argv``. Resolve a path, take the control block, and drive
it:

.. code-block:: python

   from acrobe.root import root
   from acrobe_plugin.gatecap.instrument.la.blocks.control import Control

   async def main():
       node = await root("udp/127.0.0.1:4242/gatecap")
       control, = node.children_of_class(Control)

       result = await control.capture(value=0, mask=0, count=64, pretrigger=8)

``host/capture_demo.py`` and ``host/examples/`` in the source tree are
working examples of this, including one that captures a live SPI transaction
and decodes it back with ``sigrok-cli``. The snippets below are bodies of
such a ``main()``.

A control/status panel is driven the same way, by the names its description
gave the wires:

.. code-block:: python

   from acrobe_plugin.gatecap.session import Session

   session = Session("udp/127.0.0.1:4242/gatecap")
   await session.open()

   panel = session.block_by_name("panel")
   await panel.control_write("mode", "run")     # by label, or by number
   await panel.strobe("start", "arm")           # one write: same cycle
   print(await panel.status_read("busy"))
   print(await panel.counters_read())           # events since the last reset
   await panel.reset("overflow")                # clear the flag, rebase the count

``strobe`` refuses ticks spread over several words rather than pretending a
sequence of writes is simultaneous; ``strobe_each`` is how you ask for that
sequence on purpose.

A bus explorer is the same, with a target bus behind it. Reads and writes
raise on a target that refused or never answered, a field write turns a name
into the mask the gateware does the read-modify-write with, and the journal is
what the session leaves behind:

.. code-block:: python

   session = Session("udp/127.0.0.1:4253/gatecap")
   await session.open()

   bus = session.block_by_name("dut")
   print(hex(await bus.read(0x00)))
   await bus.write_masked(0x04, value=0x2, mask=0x6)
   await bus.field_write("DEMO.CTRL", "MODE", "RUN")   # a label, or a number

   await bus.slots_set([0x00, 0x04, 0x08])   # the registers to keep live
   await bus.scan()
   print(await bus.scan_read())              # values, off the status poll

   before = await bus.snapshot("before")
   await bus.field_write("DEMO.CTRL", "ENABLE", 1)
   await bus.snapshot("after")
   print(bus.diff("before", "after"))        # address by address, field by field

   open("session.json", "w").write(bus.journal.recipe_text(bus.map_id))
   print(bus.journal.listing())

Snapshots, diffs and ``replay`` are host-side loops over the same engine, and
the pane offers none of them: what the GUI shows is the access row, the slots,
the fields and the journal.
