Elaboration-time notes
======================

The address map
---------------

Nothing in the description states an address. The map is allocated when the
rack elaborates, out of the very descriptor the host will read:

* the descriptor ROM is pinned at address 0, in a 4 KB segment, so discovery
  needs no prior knowledge on any transport;
* every instrument declares its footprint as a power of two, and the segments
  are allocated ascending by size above the ROM, each aligned on its own size.
  Routing to one is then a compare of the address bits above it;
* the address width follows the resulting extent, and the descriptor carries
  each segment's base.

The allocation is reported at elaboration, so it shows up in a simulation
transcript and in a synthesis log alike::

   twola_core_backplane.vhd:69:7:@0ms:(report note): rack segment front: base 16384, size 16384 bytes
   twola_core_backplane.vhd:69:7:@0ms:(report note): rack segment back: base 32768, size 32768 bytes

An instrument never learns its base: it decodes its own low address bits and
the descriptor is what tells the host where it sits.
