Extending gatecap
=================

This chapter is for developers. Everything before it described what gatecap
ships; this one describes the seams it leaves open, and how a project of your
own plugs new pieces into them without touching gatecap itself.

All of gatecap's extension points are keyed lookups:

* The **discovery layer** builds the host-side tree from the descriptor by
  looking up two kinds of type UUID in two driver databases: an instrument's,
  for each envelope the rack publishes, and a block's, for each child under an
  instrument. A UUID it does not know still enumerates — it just has no
  behaviour and no panel.
* The **generator** resolves everything type-specific through three
  registries: instruments by the YAML tag of an ``instruments`` entry,
  communication modes by their ``mode`` name, and signal types by the YAML tag
  of a probed signal. An unknown tag or mode is a description error listing
  what *is* registered.
* The **build** resolves VHDL dependencies through gbs repositories: a
  partition key like ``gatecap.capture`` is looked up across every repository
  the configuration lists.

A third-party feature — a protocol-aware trigger, an event counter, a bank of
build identifiers, a front end gatecap does not have — is therefore three
registrations: a driver against a UUID, a generator plugin against a name, and
a VHDL library gbs can see. All three ship from your repository; this chapter
walks through each in turn, then through the packaging that delivers them.

That path is walked: gatecap's clock-rate measurer was written as an
out-of-tree extension package of exactly this shape — its own repository, its
own ``acrobe_plugin`` namespace entry, its own gbs repository — and moved
in-tree unchanged but for names and paths, without the framework gaining
anything to host it.

Two levels, and which one you want
----------------------------------

The descriptor has exactly two levels, and so does the driver side:

An **instrument** is a user-facing feature: one entity with a single APB port,
one envelope in the rack descriptor, one node in the host tree, with a
footprint of its own in the address map, and — in the GUI — one panel, one
show/hide toggle and one status pill. gatecap's logic analyzer is one. This is
the level a new feature registers at.

A **block** is a register file *inside* an instrument: one child of that
instrument's envelope, one driver. A block never stands alone — it is
enumerated because its instrument's envelope lists it, at an offset within
that instrument's segment — and it has no surface of its own: what it offers
the user is a section of its instrument's panel.

So: something a user would name and drive is an instrument; the register files
it is built from are its blocks. A feature small enough to be one register
file is still an instrument with one child.

One plugin package
------------------

The gatecap host software is itself an acrobe plugin, and an extension is
simply *another* acrobe plugin that imports gatecap's registries. acrobe
discovers plugins by walking the ``acrobe_plugin`` PEP 420 namespace package
and importing every top-level package it finds there. Registration is a side
effect of that import: your package's ``__init__`` imports the modules that
call the ``register`` decorators, and by the time any command runs, every
installed plugin has had its say.

A minimal extension distribution looks like:

.. code-block:: none

   myext/
     setup.py                        # or pyproject.toml
     acrobe_plugin/                  # namespace package: NO __init__.py here
       myext/
         __init__.py                 # imports the registering modules
         monitor/
           __init__.py               # instrument + block drivers, adaptors
           panel.js                  # the instrument's GUI panel
         generator.py                # signal-type / communication plugins
     gateware/
       repository.gbs.yaml           # the gbs side, see below
       myext/
         library.gbs.yaml
         monitor/
           monitor.gbs.yaml
           monitor.pkg.vhd
           monitor.vhd

with a top-level ``__init__`` that does nothing but import:

.. code-block:: python

   """myext acrobe plugin.

   Importing this package registers the monitor block driver on gatecap's
   discovery layer and the !monitor signal type on its generator."""

   from . import monitor     # noqa: F401 -- registers the block driver
   from . import generator   # noqa: F401 -- registers the generator plugins

gatecap's own instruments are laid out that way too, one directory down:
``acrobe_plugin/gatecap/instrument/la/`` holds the logic analyzer's instrument
driver and its panel, its block drivers, its generator plugin, the probe types
it accepts and the ``capture`` command it adds, and it registers by being
imported from ``acrobe_plugin/gatecap/__init__.py`` -- the mechanism your
package uses, one level up. ``instrument/control_status/`` is the same shape at
a quarter of the size, and a closer model for most extensions: one register
block, one panel, one generator plugin, no command of its own.
``instrument/clock_measurer/`` is smaller still and the closest model of all:
one driver holding its own register file with no block under it, one panel fed
by the status poll, a generator plugin of one module instantiating a library
entity, and one CLI command — the whole of an instrument in five files.
``instrument/bus_explorer/`` is the widest of the small ones, and the model for
an instrument that drives something rather than reading it: one register block,
one pane, a generator plugin with a clock crossing of its own, its own CLI
subgroup, and a decoder that is entirely the driver's business. The framework
they sit on (``enumerator``, ``rack``, ``generator``, ``communication``,
``gui``, ``cli``) holds no knowledge of any of them, so all four are worked
examples of everything below.

Two rules carry over from installing gatecap itself
(:doc:`../host/install`): the ``acrobe_plugin`` directory must not contain an
``__init__.py`` (that would close the namespace and hide every other
plugin), and an editable install needs ``--config-settings
editable_mode=compat``, or the namespace walk cannot see your sources and
the plugin silently never loads.

Plugin import order is not specified. Do not rely on gatecap's own
registrations having run before yours at import time beyond what your own
imports pull in — importing ``acrobe_plugin.gatecap.enumerator`` or
``acrobe_plugin.gatecap.generator`` gives you the registries fully formed,
whichever plugin the loader happened to visit first.

An instrument driver, by UUID
-----------------------------

The envelope in the gateware
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An instrument exists, as far as the host is concerned, because the rack
descriptor carries an *envelope* for it. Its first four fields are the
framework's, frozen by the rack type UUID; whatever follows is yours, frozen
by your own type UUID:

.. code-block:: none

   [ type-uuid,   -- tag 37, your instrument type
     size_l2,     -- address-space footprint, power of two
     name,        -- instance name, free-form text
     children,    -- map: name -> [ offset | null, typed-object ]
     ... ]        -- your own fields

``gatecap.descriptor`` builds it, and the children with it:

.. code-block:: vhdl

   constant MONITOR_UUID_C : uuid_t :=
     uuid("1c0a5f38-3d61-4a2b-8f77-2b9e40c8d115");

   function monitor_envelope(size_l2 : natural) return byte_string is
   begin
     return instrument_envelope(
       type_uuid => MONITOR_UUID_C,
       size_l2 => size_l2,
       name => "mon",
       children => child_map(
         sibling_entry("counters", 0, monitor_desc(4, "a,b,c,d"))),
       t0 => cbor_tstr("counters"));
   end function;

``monitor_desc`` there is the typed object of the one block this instrument
holds, built the way the next section shows.

Child offsets are relative to the instrument's segment, and child names scope
to the instrument: a reference one child holds to another resolves in this map
alone, so two instruments may use the same names. ``size_l2`` is what the
backplane allocates a segment from — derive it and the entity's own address
decoding from one constant, and have the entity assert the two agree.

The driver on the host
~~~~~~~~~~~~~~~~~~~~~~

Instruments have a database of their own, keyed by the envelope's type UUID:

.. code-block:: python

   import uuid

   from acrobe_plugin.gatecap.enumerator import (MemoryMappedEnumerator,
                                                 MemoryMappedInstrument)

   # Must match MONITOR_UUID_C in the gateware.
   MONITOR_UUID = uuid.UUID("1c0a5f38-3d61-4a2b-8f77-2b9e40c8d115")


   @MemoryMappedEnumerator.instruments.register(MONITOR_UUID)
   class Monitor(MemoryMappedInstrument):
       def __init__(self, bridge, base, envelope):
           super().__init__(bridge, base, envelope)
           # Envelope tail: [ name of the child holding the counters ]
           self.counters_name, = self.tail
           self.counters = None

       def siblings_resolve(self, siblings):
           self.counters = siblings[self.counters_name]

The pieces of that contract:

* The constructor receives ``(bridge, base, envelope)``: the register
  transport, the base of the instrument's segment, and the decoded envelope.
  ``MemoryMappedInstrument`` keeps all three, takes the node's name from the
  envelope, and exposes ``envelope.children``, ``self.tail`` (everything past
  the four framework fields) and ``self.size`` (the footprint in bytes).
* An instrument always owns a segment, so unlike a block it never faces a
  missing base.
* Its children are enumerated for it and added under it. If it holds
  references to them — the way a logic analyzer names the capture controls one
  arm covers — it implements ``siblings_resolve(siblings)``, called once every
  child exists, with that instrument's name-to-node map.
* An instrument node is what a frontend addresses by its bare instance name,
  and its blocks by ``<instrument>.<block>``. The GUI addresses panels by the
  instrument name alone: that is the only level it knows.
* The instrument owns the whole GUI surface of what it holds: one panel for
  the instrument, none for its children.
* An instrument type with no registered driver becomes an
  ``UnknownInstrument``: its name, footprint and tail stay readable, and its
  children are still enumerated with their own drivers. A core carrying your
  instrument therefore works against a host without your plugin.

An instrument that drives registers directly rather than through children is
free to: it holds the bridge and its own base. The convention gatecap follows
is the other one — registers live in blocks, and the instrument orchestrates.

A block driver, by UUID
-----------------------

The contract in the gateware
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A block exists, as far as the host is concerned, because the core's
descriptor says so. The descriptor is a CBOR document; each block appears in
its instrument's children map as a *typed object* — an array whose first
element is the
block's type UUID (CBOR tag 37 over the 16 raw bytes) and whose remaining
elements are positional fields. There are no map keys: the field layout is
fixed by the UUID, and **any change to the layout is a new UUID**. That
freeze is what lets a host and a core of different vintages meet safely.

Mint a UUID for your block type, and build its typed object with the same
helpers ``gatecap.descriptor`` uses for the stock blocks:

.. code-block:: vhdl

   library nsl_data, gatecap;
   use nsl_data.bytestream.all;
   use nsl_data.cbor.all;
   use nsl_data.uuid.all;
   use gatecap.descriptor.all;

   constant MONITOR_UUID_C : uuid_t :=
     uuid("6f1c9a02-8d34-4b7e-9a55-0c2f81d6e743");

   -- [ type, counter-count, counter-names ]
   function monitor_desc(
     counter_count : natural;
     counter_names : string) return byte_string is
   begin
     return cbor_array(
       cbor_tagged(37, cbor_bstr(MONITOR_UUID_C)),
       cbor_positive(counter_count),
       cbor_tstr(counter_names));
   end function;

Wrap the object in a children entry — ``sibling_entry(name, offset, object)``
for a block with registers, ``baseless_sibling_entry(name, object)`` for one
that only references other blocks — and hand it to ``child_map`` along with
the entries of the blocks around it, inside the envelope of the instrument
that holds them. The descriptor fingerprint is a CRC over the whole blob, so
your block participates in change detection with no further work.

Register your block's register file inside the core's APB map following the
gatecap convention — action registers at ``0x000``, configuration at
``0x100``, status at ``0x200``, arrays at ``0x300`` — so the host-side
habits (one burst status read, contiguous config writes) keep working.

The driver on the host
~~~~~~~~~~~~~~~~~~~~~~

On connection, the enumerator decodes the descriptor and, for every child of
every instrument, looks the typed object's first element up in its driver
database. Tag 37
decodes to a Python ``uuid.UUID``, so the registration key is the same UUID,
written once more:

.. code-block:: python

   import uuid

   from acrobe_plugin.gatecap.enumerator import (
       MemoryMappedBlock, MemoryMappedEnumerator)

   # Must match the UUID in the gateware.
   MONITOR_UUID = uuid.UUID("6f1c9a02-8d34-4b7e-9a55-0c2f81d6e743")


   @MemoryMappedEnumerator.db.register(MONITOR_UUID)
   class Monitor(MemoryMappedBlock):
       REG_COUNTERS = 0x300

       def __init__(self, bridge, base, name, obj):
           super().__init__(bridge, base, name)
           # [type, counter-count, counter-names]
           _, self.counter_count, self.counter_names = obj

       async def read_counters(self):
           wb = self.bridge.word_bytes
           raw = await self.bridge.mem_read(self.base + self.REG_COUNTERS,
                                            self.counter_count * wb)
           return [int.from_bytes(raw[i * wb:(i + 1) * wb], "little")
                   for i in range(self.counter_count)]

The pieces of that contract:

* The constructor always receives ``(bridge, base, name, obj)``: the
  register transport, the block's absolute register base, its free-form
  instance name, and the decoded typed object — your positional fields,
  ready to unpack.
* The bridge is the address space the rack is mapped in, and it answers
  acrobe's memory interface: ``read32(address)`` and
  ``write32(address, value)`` for one register, ``mem_read(address, bytes)``
  and ``mem_write(address, data)`` for a run of them in one burst. It also
  carries ``word_bytes``, the width of a word in the map. Addresses are
  absolute — ``self.base + offset`` — and every access is word-aligned and
  word-complete.
* Derive from ``MemoryMappedBlock`` when the block owns registers; it holds
  the bridge and the base, and refuses a descriptor entry with no base. A
  reference-only block (a baseless sibling entry) derives from
  ``acrobe.node.Node`` directly and is built with ``base=None``.
* A driver holding references to its instrument's other children by name —
  the way a capture control names its buffer and trigger — implements
  ``siblings_resolve(siblings)``. It is called once every child of the
  instrument exists, with the name-to-node map, so cross-references bind
  while the whole set is known.

The database has a default: a UUID with no driver becomes an
``UnknownComponent`` that still shows up in ``info`` output. That cuts both
ways, deliberately — a core carrying your block works against a host without
your plugin, and your plugin does nothing to cores that lack the block.

An instrument's user interface
------------------------------

Drivers are UI-agnostic. Both front ends discover a node's UI through one
seam: the driver exposes ``ui_adaptor(frontend, resources=None)`` and returns
an adaptor for ``"console"`` or ``"gui"``, or ``None`` for a frontend it does
not serve — the node is then simply skipped by that front end. The framework
talks only to the adaptor, never to the driver, which is what lets a
third-party instrument ship its own UI alongside its driver.

The two front ends see different levels, on purpose:

* the **console** describes nodes, so every node may answer: an instrument
  and each of its blocks in turn, in ``acrobe gatecap info``;
* the **GUI** drives hardware, so only instruments answer. One instrument is
  one panel, one show/hide toggle in the top bar and one status pill inside
  that toggle. A block returning a GUI adaptor is never asked for one — two
  surfaces over one piece of hardware could only be a way to disagree with
  each other, and the instrument is the level a user names.

Console
~~~~~~~

A ``ConsoleAdaptor`` describes the node in ``acrobe gatecap info`` (and in
the GUI's log pane on connect). ``info()`` returns lines of text built from
the descriptor alone — no hardware round-trips:

.. code-block:: python

   from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor


   class MonitorConsole(ConsoleAdaptor):
       def info(self):
           d = self.driver
           return [f"{d.name}:",
                   f"  counters ({d.counter_count}): "
                   + ", ".join(d.counter_names)]

GUI
~~~

A GUI panel is two halves: a ``GuiAdaptor`` on the *instrument* driver, and a
``panel.js`` it serves to the shell. The shell asks each instrument for its
self-description, loads the panel script the adaptor points at, and renders
one pane per instrument, top to bottom by ascending ``ORDER``.

.. code-block:: python

   from importlib.resources import files

   from acrobe_plugin.gatecap.frontend.adaptor import GuiAdaptor


   class MonitorGui(GuiAdaptor):
       PANEL = files(__package__).joinpath("panel.js")
       ORDER = 50

       def describe(self):
           d = self.driver
           meta = {"name": d.name, "type": str(MONITOR_UUID),
                   "counter_names": d.counters.counter_names}
           meta["key"] = self.panel_key(meta)
           return meta

       async def message(self, msg):
           if msg.get("op") == "read":
               return {"counters": await self.driver.counters.read_counters()}
           raise ValueError(f"unknown op {msg.get('op')!r}")

* ``describe()`` is the instrument's entry in the manifest the shell renders
  from. It must carry ``name``, ``type`` (the UUID string the shell routes
  panels by) and ``key`` — ``panel_key(meta)`` hashes the description so
  saved settings follow the gateware instrument, not the transport it was
  reached over. Everything else is yours: whatever the panel needs to render
  without a round-trip belongs here, including what its blocks have to say
  about themselves (the logic analyzer publishes its capture domains and the
  fields of each trigger block it holds this way).
* ``resource(name)`` serves named assets under the instrument's per-instance
  URL namespace; the base class serves ``PANEL`` as ``panel.js``, and
  ``panel_url()`` mints the URL the shell fetches it from. Override
  ``resource`` for anything else — an image, a rendered trace — and delegate
  to ``super()`` for the script.
* ``message(msg)`` is the panel's back-channel: every UI action arrives
  here as a JSON value and the return value goes back to the panel. One
  operation name is special: a successful ``{"op": "configure", ...}`` is
  cached by the framework and replayed after a transport reconnect, so
  persistent register state (the way a trigger compare survives a cable
  glitch) is restored without the panel's involvement. Use ``configure``
  for exactly that kind of state and nothing else — and, since the framework
  keeps the last one, have it carry all of that state at once (gatecap's
  analyzer sends every trigger it holds in one message, whichever one the
  user edited).

Finally the driver hands them out, caching one adaptor per frontend:

.. code-block:: python

   class Monitor(MemoryMappedInstrument):
       ...

       def ui_adaptor(self, frontend, resources=None):
           cached = self.__dict__.get(f"ui_{frontend}")
           if cached is not None:
               return cached
           if frontend == "gui":
               adaptor = MonitorGui(self, resources)
           elif frontend == "console":
               adaptor = MonitorConsole(self)
           else:
               return None
           self.__dict__[f"ui_{frontend}"] = adaptor
           return adaptor

The panel script is loaded once per instrument *type* and self-registers
against the same UUID; the shell then instantiates it once per instrument:

.. code-block:: javascript

   (function () {
     const MONITOR_UUID = "1c0a5f38-3d61-4a2b-8f77-2b9e40c8d115";

     window.gatecap.registerPanel(MONITOR_UUID, {
       async render(ctx, el, instrument) {
         el.innerHTML = `<div class="pane-controls">`
           + `<span><span class="name">${instrument.name}</span>`
           + `<span class="kind">monitor</span></span>`
           + `<pre class="mon"></pre></div>`;
         const r = await ctx.send({op: "read"});
         el.querySelector(".mon").textContent =
           instrument.counter_names
             .map((n, i) => `${n}: ${r.counters[i]}`).join("\n");
       },
     });
   })();

The UUID it registers against is the *instrument* type's — the one the
envelope carries — since that is what the shell routes panels by.

``render(ctx, el, instrument)`` receives the pane's DOM element and the
instrument's ``describe()`` output. ``ctx`` is the panel's whole world:
``ctx.send(msg)`` routes to the adaptor's ``message()``, ``ctx.settingsGet()``
/ ``ctx.settingsSet(s)`` persist the panel's UI state under the instrument's
key — one settings object for the whole panel, so its sections must merge
rather than overwrite — ``ctx.state`` holds transient pane state,
``ctx.log(text)`` writes to the shell's log pane, and ``ctx.Waveform`` embeds
a Surfer waveform view for a panel that shows traces. An optional
``onStatus(ctx, instrument, status)`` on the same object is called on every
status poll tick (see below). A panel never touches another instrument: all
I/O is routed through its own ``ctx``.

A panel with several sections — an editor per trigger block, a capture, a
waveform — rebuilds only the section that changed, never the whole pane: a
re-render would take the waveform surface with it.

Status, and the pill the shell shows for it
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A panel that defines ``onStatus`` opts its instrument into the shell's poll
loop: once per tick the shell calls the instrument driver's ``async poll()``,
hands the result to ``onStatus``, and renders a status pill from it inside
that instrument's show/hide button in the top bar — so an instrument reports
even while its panel is hidden, and several instruments each report their own
state instead of one of them speaking for the rack.

``poll()`` is the whole contract, and it is self-describing: the framework
neither maps nor interprets what it carries.

.. code-block:: python

   async def poll(self):
       return {"state": "measuring",       # short label, shown in the pill
               "tone": "active",           # how the shell colours it
               "fingerprint": await self.fingerprint(),
               "rate": self.last_rate}     # anything else is the pane's own

* ``state`` is a short human label of your own choosing — the shell prints it,
  it never decodes it. Keep any numeric state encoding inside your package
  (gatecap's capture cores keep theirs in the logic-analyzer package, where
  the console and the CLI share the same names).
* ``tone`` is one of a small fixed vocabulary the shell has styling for:
  ``idle`` (nothing to do), ``active`` (work in flight), ``attention``
  (something the user is waiting for happened), ``error`` (failed). It is a
  severity, not a state name, so a new instrument expresses itself without a
  line of shell CSS. A poll with no tone is shown as ``idle``.
* ``fingerprint`` is **required**: the rack's change detection, link health
  and round-trip time all ride on the polls the panels make, so every poll
  must carry the instance fingerprint (the value ``fingerprint()`` returns). A
  driver that answers a poll from host memory — the way a capture control does
  while its trace is being read back — reports the last one it read.
* Everything else is between the driver and its own panel. gatecap's analyzer
  sends ``triggered``, ``progress`` and a readback ``fetch`` snapshot; the
  shell compacts the first two into the pill and its tooltip.

An instrument built from several blocks answers one poll for all of them: it
polls what it needs and composes the answer (the logic analyzer polls every
capture domain and reports the busiest state, the progress of whichever domain
is holding the group up, and one readback fraction over the whole transfer).
That way one instrument stays one pill, and one pill stays one round trip.

The framework adds ``changed`` (the fingerprint no longer matches the one read
at connect), ``health`` and ``latency_ms`` to the dict before the panel sees
it, and drives the connection LED, the round-trip readout and the stale banner
from them. On a poll that fails it rebuilds the transport and retries, adding
``reconnecting``, ``reconnected`` and ``replayed`` — a panel may ignore all of
those.

The same ``fingerprint()`` protocol is what the session seeds itself from at
connect: any node exposing it takes part, whichever instrument it belongs to.

A panel that renders a waveform can reuse the ``WaveformView`` mixin on the
adaptor side (``acrobe_plugin.gatecap.instrument.la.waveform``) — the VCD
authoring, bus grouping and trigger markers the stock capture panel uses —
rather than reimplementing trace rendering.

What a block publishes to its instrument's panel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A block with something to show does not grow a UI: it publishes what its
instrument's panel needs, in plain driver methods, and the instrument's
adaptor places it. gatecap's trigger blocks are the model — a ``describe()``
returning the fields an editor renders (descriptor data, no round trip) and an
``async apply(params)`` writing the compare that editor holds and returning a
one-line summary:

.. code-block:: python

   class Trigger(MemoryMappedBlock):
       KIND = "value"

       def describe(self):
           return {"name": self.name, "kind": self.KIND,
                   "fields": trigger_fields(self.signal_names)}

       async def apply(self, params):
           value, mask = params.get("value", 0), params.get("mask", 0)
           await self.configure(value, mask)
           return f"value={value:#x} mask={mask:#x}"

The instrument gathers those into its ``describe()`` and routes its panel's
ops back to them. Two flavours of the same block (a value and an edge trigger)
publish the same shape with a different ``kind``, so one panel renders both
and the instrument stays ignorant of which it holds.

A generator driver, by name
---------------------------

The generator (:doc:`../usage/description`) resolves the type-specific parts of
a description through three registries in
``acrobe_plugin.gatecap.generator``, and all of them accept third-party
entries at import time — register from your plugin's ``__init__`` chain and
every ``acrobe gatecap generate`` run sees them.

Signal types, keyed by YAML tag
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A signal-type plugin owns everything specific to one probe type: the keys it
accepts, the ports and generics it adds to the generated entity, the
elaboration-time expressions that pack it into the capture and trigger
vectors, and the gbs dependencies the generated partition must list.
Subclass ``SignalTypePlugin`` and register the class:

.. code-block:: python

   from acrobe_plugin.gatecap.generator import (
       SignalTypePlugin, SignalTypeRegistry)


   @SignalTypeRegistry.register
   class MonitorBusSignal(SignalTypePlugin):
       TAG = "!monitor-bus"
       KEYS = ("width",)

       ...

A description may then write ``probe: !monitor-bus {width: 4, trigger:
true}`` and your class answers for it. The interface, in the order the
pipeline calls it:

``parse(payload, path)``
   Validate the type-specific keys (``KEYS``) and return them as the probe's
   params. Raise ``DescriptionError`` with the offending path for anything
   wrong — all description errors die here, before any VHDL exists.

``parse_trace(value, path)`` / ``parse_trigger(value, path)``
   How the common ``trace`` / ``trigger`` keys read for this type. The base
   class implements the whole-signal convention (``trace: false`` opt-out,
   ``trigger: true`` opt-in); an abstract bus type overrides them to accept
   an element-selection string instead.

``ports(probe)`` / ``generics(probe)``
   The entity ports and generics this probe contributes.

``length(probe, selection)`` / ``pack(probe, selection)`` / ``names(probe, selection)``
   VHDL expressions for the selected bits' count, their packing into the
   vector, and their name-spec fragment. ``static_width`` returns the bit
   count when the description alone decides it, or ``None`` when only
   elaboration can.

``deps(probe)``
   gbs partition keys the generated code needs when this probe is present —
   this is where your own gateware library enters the emitted manifest
   (below).

Follow the packer-package convention the stock abstract types use: back the
type with a VHDL package exposing a ``*_length`` / ``*_pack`` / ``*_names``
function triad driven by one element-key string, and have ``length``,
``pack`` and ``names`` emit calls to it. Vector layout and name-spec are
then computed by the same VHDL code at elaboration, and cannot drift from
what the generator believed.

Communication modes, keyed by name
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A communication plugin owns the host-facing transport of a generated core:
subclass ``CommunicationPlugin``, set ``MODE`` to the name descriptions
select it by, and register it:

.. code-block:: python

   from acrobe_plugin.gatecap.generator import (
       CommunicationPlugin, CommunicationRegistry)


   @CommunicationRegistry.register
   class MyLinkCommunication(CommunicationPlugin):
       MODE = "mylink"
       KEYS = ("max_rate",)

       ...

The contract is declarative: the transport logic is a library entity — an
*adapter* — and the plugin only says which one and how it is bound. ``UNIT``
names the adapter, ``ports()`` and ``generics(context)`` are the boundary
interface, forwarded to it formal to formal, ``generic_map(context)`` binds
what is left, and ``deps()`` lists the partitions it pulls. Every adapter
speaks the same inner contract — an APB requester of a given configuration, a
clock and a reset, and a ``descriptor_base_c`` generic — so a mode whose
``UNIT`` is ``None`` is a passthrough: the rack hands its APB out as its own
port pair.

A mode owns its half of the ``communication`` section. ``KEYS`` names the keys
it adds to the framework's own ``mode`` and ``clock``, and ``parse(section,
path)`` — a classmethod — validates them and returns what the plugin wants to
read back, which the context then carries as ``params``:

.. code-block:: python

   @classmethod
   def parse(cls, section, path):
       rate = Field.integer(section, "max_rate", path, minimum=1)
       if rate is None:
           raise DescriptionError("max_rate is required", f"{path}.max_rate")
       return {"max_rate": rate}

The parser reads ``mode`` first and only then checks the section's keys, so
they are checked against *your* mode's set: a key another mode owns is a
description error naming the keys known here, and one of yours is accepted
nowhere else. A mode with nothing of its own declares neither — ``KEYS``
defaults to ``()`` and ``parse`` to an empty dict, which is every shipped mode
but ``spi``, whose ``max_rate`` and ``spi_mode`` are the only description keys
a transport takes today.

The ``CommunicationContext`` a plugin receives names the host clock and reset,
the APB configuration constant, the APB signal pair, the descriptor's byte
address, the host clock rate in Hz (zero when the description states none), an
expression evaluating to the rack's descriptor fingerprint — for a link that
publishes an identity of its own, as ``usb`` does with its serial-number
string — and ``params`` as ``parse`` returned it; that surface is all a
transport may reach.

``check(context)`` is the hook for what only that assembled context can tell.
``parse`` sees the ``communication`` section alone, but the host clock's rate
comes from the instrument exporting the clock, so a mode that can only serve
certain rates checks them here and raises ``DescriptionError`` like any other
description fault — which is what ``usb`` does, its transceiver having a
bit-recovery loop for two rates and no others.

A transport with no host clock of its own would set
``CLOCKED = False``, which makes the parser reject a
``communication.clock`` key for it — no shipped mode does, JTAG included: the
TAP transport terminates the protocol on TCK but crosses into the host clock
in its own FIFOs, so it is a host-clock unit like the rest.

Emission goes through the generator's small VHDL code model, not through text
templates. It lives in ``acrobe_plugin.gatecap.generator.vhdl`` and is
re-exported by the generator package, so one import gives you both the base
classes and the nodes:

.. code-block:: python

   from acrobe_plugin.gatecap.generator import (Constant, Expr, Generic,
                                                Instance, Port)

Instruments, keyed by tag
~~~~~~~~~~~~~~~~~~~~~~~~~

An *instrument* is a user-facing feature of a core: one entity behind one APB
port, described by one entry of the description, enumerated as one node on
the host. A build-identifier block, a counter bank or a stimulus generator
belongs here.

Every description is a **rack**: the generator emits a package (the functions
the address map and the descriptor are built by), a backplane (the descriptor
ROM, the router and the instruments) and a rack entity (the transport over the
backplane). gatecap's own logic analyzer is one instrument among them, on this
very contract.

Subclass ``InstrumentPlugin``, set ``TAG`` to the tag descriptions select it
by, and register the class:

.. code-block:: python

   from acrobe_plugin.gatecap.generator import (
       InstrumentPlugin, InstrumentRegistry)


   @InstrumentRegistry.register
   class ClockCounter(InstrumentPlugin):
       TAG = "!clock-counter"
       KEYS = ("clocks",)

       ...

A description then embeds instances of it under a top-level ``instruments``
section. The mapping key is the *instance name*, the tag picks the plugin:

.. code-block:: yaml

   name: mypkg.myrack
   communication:
     mode: apb
   instruments:
     rates: !clock-counter
       clocks: 2

The instance name is the instance's whole identity: the name the host
enumerates it under, and the prefix of every port, signal and constant it
brings — so ``rates`` above yields ports ``rates_clock_0_i`` and
``rates_clock_1_i``. It must be a VHDL identifier; instances keep description
order everywhere, the descriptor included.

The interface, in the order the pipeline calls it:

``parse(payload, path)``
   Validate the instance's keys (``KEYS``, checked for you before this
   runs) and return them as the instance's params, reachable afterwards as
   ``instrument.params``. Raise ``DescriptionError`` with the offending path
   for anything wrong — like everything else, an instrument's description
   errors die before any VHDL exists.

``ports(instrument)`` / ``generics(instrument)``
   The boundary ports and generics this instance bubbles up to the rack.
   Name them through ``instrument.port(suffix)`` and
   ``instrument.constant(suffix)`` so two instances of the same instrument
   never collide; the parser claims every port name, and a clash with the
   core's own ports is reported as a description error.

``clocks(instrument)`` / ``clock_rates(instrument)``
   Exported clocks: a mapping of clock name to the boundary port carrying
   it. ``communication.clock`` names one as ``<instance>.<clock>``, and the
   framework binds that very port where the clock is used — a clock is never
   re-driven through a signal assignment, which would add a delta cycle and
   skew its domain. ``clock_rates`` states the rate of those the description
   fixes, in Hz, which is what a transport dividing the host clock down (the
   serial one) takes instead of asking for a generic.

``constants(context)``
   Constants the generated package declares for this instance. Both the
   envelope and the instantiation are built from them, which is what keeps
   the two from disagreeing.

``envelope(context)`` / ``envelope_declarations(context)``
   A VHDL ``byte_string`` expression building the instance's descriptor
   envelope, on ``gatecap.descriptor.instrument_envelope``: the type UUID,
   the address-space footprint as a power of two, the instance name, the
   children map, then the fields the instrument type defines. The rack wraps
   the expression in a package function taking this instance's generics, so
   an envelope whose geometry only elaboration knows is expressible;
   ``envelope_declarations`` are that function's local constants. It is a
   function even when the instance takes no generic — the emission has one
   shape, and the caller is a function call either way. The
   footprint is what the rack allocates a segment from, so derive it and the
   entity's own address decoding from one constant, and have the entity
   assert that the footprint it is handed matches what it decodes.

``instance(context)`` / ``components(context)``
   The instantiation of the instrument's entity, as one ``Instance``, and the
   component declarations the generated package carries for whatever the
   plugin emitted.

``files(context)`` / ``deps(instrument)``
   Files the plugin writes next to the generated rack, in analysis order
   (empty for an instrument taken from a library), and the gbs partition keys
   the rack needs when this instance is present — where your own gateware
   library enters the emitted manifest (below).

An instrument never learns its own base: the ``InstrumentContext`` handed to
``constants``, ``envelope``, ``instance``, ``components`` and ``files`` names
the ``Instrument`` entry itself, the host clock and reset, the APB
configuration constant, the requester and completer signals of *its* segment,
the constant holding the word width, the fingerprint constant, the host clock
rate in Hz (zero when the description states none), the rack entity's name
(to name whatever the plugin emits), the footprint expression read back from
its own envelope, and the exported clock of its own the rack runs on, if any.
That surface is all an instrument may reach.

.. code-block:: python

   from acrobe_plugin.gatecap.generator import Instance

   ...

   @classmethod
   def instance(cls, context):
       instrument = context.instrument
       return Instance(
           instrument.label("counter"), "myext.clock_counter.clock_counter",
           generic_map={"apb_config_c": context.apb_config,
                        "size_l2_c": instrument.constant("size_l2"),
                        "channel_count_c": instrument.constant("channels")},
           port_map={"clock_i": context.clock,
                     "reset_n_i": context.reset_n,
                     "apb_i": context.apb_master,
                     "apb_o": context.apb_slave})

What an instrument needs but should not reinvent is in the framework, because
two instruments of unrelated shape need the same thing:

``Cdc`` / ``ClockDomain``
   The crossings between the host clock and a clock of your own — an event
   tick, a static register for a set-and-hold mask, a resynchronised register
   for a level — and their collapse to a plain alias when the two domains turn
   out to be one. The in-tree instruments that straddle two clocks cross that
   way: an instrument
   should not be emitting ``nsl_clocking.interdomain`` instances by hand.

``EnumSpec``
   The ``value: label`` table a description may attach to a field, and the
   ``<...>`` name-spec suffix it renders into the descriptor. Its host-side
   counterparts, ``acrobe_plugin.gatecap.names.SignalNames`` and
   ``acrobe_plugin.gatecap.enums.EnumTable``, read back what it emits — a
   probe vector and a register list are written in the same grammar, so a
   driver parses names with the framework's parser rather than its own.

``Field``
   Typed access to a description mapping (identifiers, integers with bounds,
   known-key checks) raising ``DescriptionError`` with the offending path.

The host side of this is the instrument driver above: one node per envelope,
bound by the instrument's type UUID, with its children bound by theirs. A
description holding no instrument at all is rejected — a rack needs at least
one.


Shipping gateware to gbs
------------------------

The third leg is the VHDL itself: the packer package behind your signal
type, the front end behind your mode, the RTL of your block. It ships as a
gbs *repository* — a tree of libraries and partitions — that the user's
build configuration points at.

The layout is the yaml-loader shape gatecap's own library uses. Three
levels, each a small manifest:

.. code-block:: yaml

   # gateware/repository.gbs.yaml
   name: myext_impl
   description: myext gateware
   libraries:
     - path: myext

.. code-block:: yaml

   # gateware/myext/library.gbs.yaml
   name: myext
   partitions:
     - monitor/monitor

.. code-block:: yaml

   # gateware/myext/monitor/monitor.gbs.yaml
   sources:
     - file_type: vhdl
       files:
         - monitor.pkg.vhd
         - monitor.vhd
   deps:
     - gatecap.descriptor
     - nsl_data.cbor

The repository defines library ``myext``; anything may then depend on the
partition key ``myext.monitor``, and that key is exactly what your
signal-type plugin's ``deps()`` returns, so a generated core that uses your
type emits a manifest pulling your gateware in by name.

For the key to resolve, the repository must be listed in the gbs
configuration. Two places work, and they merge:

* the tree configuration — the ``.gbs.yaml`` found walking up from the
  build directory — for a per-project choice;
* ``~/.config/gbs.yaml`` for a per-user one, applying to every tree.

.. code-block:: yaml

   # .gbs.yaml (or ~/.config/gbs.yaml)
   repositories:
     - path: ../myext/gateware/repository.gbs.yaml
       loader: yaml

Relative paths resolve against the file that states them, so a tree
configuration can name a sibling checkout and a user configuration an
absolute install location. Have your distribution's install instructions
state this one entry; it is the only per-user step the gateware side needs.

gbs also has a plugin namespace of its own — ``gbs.plugin.*`` packages
exposing a ``gbs_register()`` — but that mechanism contributes *tools*:
synthesis backends, repository loaders, toolchain discovery. Shipping VHDL
sources needs no gbs plugin, only the repository entry above.

Checklist
---------

A complete extension, end to end:

* Mint one UUID per instrument type and one per block type; write the layout
  down next to each, in both the VHDL package and the driver, and treat it as
  frozen — a changed layout is a new UUID.
* Gateware: an entity with a single APB port, its envelope function and the
  children entries of its register files, each on the gatecap map convention;
  a gbs library carrying it.
* Host: one acrobe plugin package under ``acrobe_plugin/``, registering the
  instrument on ``MemoryMappedEnumerator.instruments`` and each block driver
  on ``MemoryMappedEnumerator.db`` at import.
* UI, when a node warrants one: a console adaptor for ``info``, a GUI adaptor
  plus ``panel.js`` registering against the same UUID.
* Generator, when descriptions should name your type: an
  ``InstrumentRegistry``, ``CommunicationRegistry`` or ``SignalTypeRegistry``
  entry whose ``deps()`` names your gbs partitions.
* Install: ``pip install`` (editable installs with ``editable_mode=compat``)
  and one ``repositories:`` entry in the gbs configuration.

Verify the round trip the same way gatecap verifies itself: a socket bench
instantiating a rack that holds your instrument (:doc:`simulation`), and
``acrobe gatecap info`` against it — your instrument and its blocks listed
with their own description lines is all three registrations working at once.
