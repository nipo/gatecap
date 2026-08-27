Describing the rack
===================

Every gatecap core is generated from custom needs.

User writes one YAML file naming what the core holds: the link it
answers on, the instruments in it, their clocks and the signals they
watch. The generator emits the VHDL: the probe concatenation, the
names, the clock-domain crossings, the address map, the
self-description and the transport, as one entity that will be
instantiated like any other component.

That entity is a **rack**: a transport adapter over a backplane
holding one or more instruments.

The document
------------

The whole rack is one YAML document. This one holds a single logic analyzer
with two domains on unrelated clocks, correlated by one trigger:

.. code-block:: yaml

   name: rack_partition.demo_rack

   communication:
     mode: axi4_stream
     clock: la.control

   instruments:
     la: !logic-analyzer
       storage:
         buffer_depth_l2: 8

       capture:
         max_windows: 1

       trigger:
         capabilities: value

       domains:
         control:
           clock: clock
           frequency: 100_000_000

           signals:
             command: !axi4-stream
               trace: dvlr
               trigger: vl
             state: !bus
               width: 2
               trigger: true
               enum:
                 0: IDLE
                 1: BUSY
                 2: HOLD
                 3: DONE
             count: !bus
               width: 8

         phy:
           clock: clock
           frequency: 125_000_000
           trigger:
             from: control

           signals:
             word: !bus
               width: 8
             mark: {}

Three sections, and only three: ``name``, ``communication`` and
``instruments``. Everything about capture — storage, trigger, domains,
signals — lives *inside* an instrument, because it belongs to the logic
analyzer rather than to the rack.

Name
----

``name`` is a dotted ``package.entity`` pair. The first half names the
emitted package, the file holding it and the gbs partition; the second names
the rack entity and its file. The library is yours — the generated partition
does not name one. The two halves must differ, since VHDL forbids reusing the
package name inside it.

Ports are named after the description, instrument instance first: a signal
becomes ``<instance>_<domain>_<signal>_i``, and every domain gets
``<instance>_<domain>_reset_n_i``. Bus and stream fields keep their hierarchy
in the *names* the host reads (``command.data[3]``), which the waveform viewer
turns into scopes.

Instruments
-----------

Each entry under ``instruments`` is one instance: the key names it,
the YAML tag selects the instrument type, and the body holds that
type's own keys. A rack needs at least one. :doc:`../instrument/index`
lists each type, keys it takes, and the ports it brings.

The instance name is the instance's whole identity — the prefix of every port
it brings, the name the host enumerates it under, and the key of its APB
segment — so it must be a VHDL identifier and be unique. Two logic analyzers
in one rack are legal and mean two independent arm groups:

.. code-block:: yaml

   name: rack_partition.demo_rack

   communication:
     mode: apb

   instruments:
     front: !logic-analyzer
       storage:
         buffer_depth_l2: 6
       domains:
         control:
           clock: clock
           frequency: 100_000_000
           signals:
             state: !bus
               width: 4
               trigger: true

     back: !logic-analyzer
       storage:
         buffer_depth_l2: 10
         packed: true
       domains:
         control:
           clock: clock
           frequency: 50_000_000
           signals:
             word: !bus
               width: 8
               trigger: true

Both analyzers hold a domain called ``control``; their ports still differ
(``front_control_state_i``, ``back_control_word_i``), and so do the names the
host shows. An unknown tag is a description error naming the tags that *are*
registered.

See :doc:`../instrument/index` for catalog of instruments.

Communication
-------------

``mode`` picks how the host reaches the rack, and it is the one thing
about a rack that is a wire rather than a register: everything above
it is the same on all seven.

Each mode's page shows its ``communication`` stanza as an excerpt. The
document such a stanza rides is the smallest rack there is — one logic
analyzer, one probe:

.. code-block:: yaml

   name: rack_partition.demo_rack

   communication:
     mode: axi4_stream
     clock: la.sample

   instruments:
     la: !logic-analyzer
       domains:
         sample:
           frequency: 100_000_000
           signals:
             state: !bus
               width: 4
               trigger: true

See :doc:`../communication/index` for catalog of communication channels.
