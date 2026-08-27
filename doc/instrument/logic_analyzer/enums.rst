Enumerated values
=================

A field that carries a state, a response code or an opcode is easier to read
as a name than as a number. A described signal may carry a value table, and
the host then shows the label instead of the number, in the waveform, in CSV
output and in the trigger editor.

.. code-block:: yaml

   signals:
     state: !bus
       width: 2
       enum:
         0: IDLE
         1: START
         2: RUN
         3: DONE
     error: {}

``state`` now reads ``IDLE``/``START``/``RUN``/``DONE`` everywhere, and a
trigger term can be written ``state=RUN``.

In the descriptor the table rides the field's name as an angle-bracket suffix,
``state[0:1]<IDLE,START,RUN,DONE>,error``, and that spelling is what the rest
of this chapter describes: it is what you read in ``info`` output, and what a
hand-written spec has to produce.

Writing a table
---------------

The table is a comma-separated list, applied left to right onto a running
index that starts at 0:

``label``
   Assign the label to the running index, then advance it. A bare list is
   therefore just "values from 0 up".

``N:label``, ``0xNN:label``
   Assign the label to value ``N``, and continue from ``N+1``. Use it to skip
   holes, or to start somewhere other than 0.

``+namespace.name``
   Splice in a well-known table (see below), then continue past its highest
   value.

*(empty)*
   Leave the running index unmapped and advance. Useful to skip one value in
   an otherwise dense list.

Later entries win, so an entry after a splice overrides what the splice
brought in. Values with no label render as numbers.

An enum belongs to one field. Attaching one to a brace group is an error —
write the group out and tag the field that needs it.

Inheriting a table
------------------

Common encodings are pre-declared on the host and referenced by name, so the
spec stays short and every core in the design says the same thing. A
description names one under the reserved ``base`` key:

.. code-block:: yaml

   resp: !bus
     width: 2
     enum: {base: axi.resp}          # OKAY, EXOKAY, SLVERR, DECERR

The shipped tables are ``axi.resp``, ``axi.burst`` (FIXED, INCR, WRAP),
``axi.size`` (1B, 2B, 4B, 8B, …) and ``axi.lock`` (NORMAL, EXCLUSIVE).

A base can be adjusted and extended in place — later entries win, so a value
of your own overrides what the base brought in:

.. code-block:: yaml

   # Reuse the AXI response codes, but call value 2 something of ours
   resp: !bus
     width: 2
     enum: {base: axi.resp, 2: BUS_TIMEOUT}

   # Reuse a base and extend it above its own values
   phase: !bus
     width: 8
     enum:
       base: demo.phase
       3: custom0
       4: custom1
       5: STOP

The second one maps 0..2 from the ``demo.phase`` base, then ``custom0`` at 3,
``custom1`` at 4, ``STOP`` at 5, and leaves 6..255 unmapped — those render as
their value. In the descriptor it reads
``phase[0:7]<+demo.phase,3:custom0,4:custom1,5:STOP>``; a base a description
names but the host does not know is a description error.

The well-known tables live on the host side, in the gatecap acrobe plugin
(``acrobe_plugin/gatecap/enums.py``). Adding a shared table for
your own project's encodings is a small edit there; anything
project-specific and one-off is better written inline in the spec, where it
travels with the bitstream.

Where the labels show up
------------------------

* **Waveform** — the bus displays the label instead of a hexadecimal value.
* **CSV** — the cell holds the label, falling back to hex for unmapped
  values.
* **Trigger** — a term may name a label, ``--trigger state=RUN``, and the
  GUI's trigger editor offers the labels in a drop-down for that field.
* **Panel widgets** — a control/status register carrying a table is a
  drop-down of its labels rather than an entry, and a status shows its label
  (:doc:`../index`).

A described signal carries one table, which lands in every vector the signal
takes part in, so a field triggered on and traced reads by label on both
sides.
