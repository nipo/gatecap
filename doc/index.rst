gatecap
=======

gatecap is an embedded logic analyzer for FPGA designs: a capture core you
instantiate in your own gateware, and a host program that connects to it,
arms it, and shows you the waveform.

It is meant to replace the vendor-supplied embedded analyzers (ChipScope,
SignalTap, Gowin Analyzer Oscilloscope, Reveal, …) with one tool that works
the same way on every vendor, over whatever link you already have to the
board, and that is driven from a scriptable command line as readily as from
a window.

.. figure:: images/gatecap-gui.png
   :alt: The gatecap window, capturing an SPI transaction
   :width: 100%

   A capture of an SPI transaction: connection bar, trigger editor, capture
   controls and waveform, all built from what the core said about itself.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   overview
   instrument/index
   communication/index
   usage/index
   host/index
   developer/index
