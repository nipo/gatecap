"""Bus-explorer instrument driver: an APB master the host drives.

The instrument binds on the envelope UUID, reads the target's dimensions out of
the envelope tail and drives everything through its one child, the engine
register file. It is the only node a caller talks to.

Every target access is indirect and fire-then-poll: the operation is staged in
the config region and fired by a COMMAND write, and STATUS says when it
completed and how. There is no pass-through aperture, so a target that never
answers costs one timeout and nothing else -- which is exactly the situation
the instrument exists for.

Three things the driver adds over the register file:

* decode. The descriptor names a register map, never carries one; the host
  resolves that name against the SVD documents the user registered
  (:mod:`.svd`) and answers raw hex when it has none. A field write is where
  decode earns its keep: name a field, and the mask comes out of the map and
  the read-modify-write out of the gateware.
* the journal. Every access the node performs goes through one hook, and the
  writes among them are kept with the names the map gave them
  (:mod:`.journal`). A session exports a listing and a recipe it can replay.
* snapshots. Sweeping a range or the slot set into a named snapshot, and
  diffing two of them, are host-driven engine loops -- nothing in the gateware
  knows about them.

Everything live rides the status poll: the engine's own state, the scan flags
and one value per slot, in one burst read, so the pill and the registers of
interest cost the transport one round trip a tick.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from importlib.resources import files

from acrobe_plugin.gatecap.enumerator import (MemoryMappedEnumerator,
                                              MemoryMappedInstrument)
from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor, GuiAdaptor

from .blocks.engine import BusExplorerEngine
from .journal import Journal
from .svd import MapLibrary, SvdDocument, SvdError

# Must match BUS_EXPLORER_UUID_C in the gateware (gatecap.bus_explorer).
BUS_EXPLORER_UUID = uuid.UUID("5804305e-b62b-400f-94e3-86c905d87b97")


class BusAccessError(Exception):
    """A target access the engine completed with an error code. What went
    wrong is the class, so a caller distinguishes a target that is not there
    from one that refused."""

    CODE = None

    def __init__(self, op, address, message):
        super().__init__(message)
        self.op = op
        self.address = address

    @classmethod
    def of(cls, code, op, address):
        """The exception a STATUS error code names."""
        for kind in (BusTimeout, BusSlaveError, BusCommandError):
            if kind.CODE == code:
                return kind(op, address)
        raise AssertionError(f"error code {code} is not one of the four the "
                             f"register map defines")


class BusTimeout(BusAccessError):
    CODE = 1

    def __init__(self, op, address):
        super().__init__(op, address,
                         f"{op} of {address:#x} timed out: the target did not "
                         f"answer within the engine's timeout")


class BusSlaveError(BusAccessError):
    CODE = 2

    def __init__(self, op, address):
        super().__init__(op, address,
                         f"{op} of {address:#x} was refused by the target "
                         f"(pslverr)")


class BusCommandError(BusAccessError):
    CODE = 3

    def __init__(self, op, address):
        super().__init__(op, address,
                         f"{op} is not an operation the engine implements; no "
                         f"target access was made")


@dataclass
class Snapshot:
    """A set of target values read at one moment, under a name."""

    name: str
    time: float
    values: dict = dataclass_field(default_factory=dict)
    errors: dict = dataclass_field(default_factory=dict)

    def addresses(self):
        return sorted(set(self.values) | set(self.errors))

    def record(self):
        return {"name": self.name, "time": self.time,
                "values": {str(a): v for a, v in sorted(self.values.items())},
                "errors": {str(a): e for a, e in sorted(self.errors.items())}}


@MemoryMappedEnumerator.instruments.register(BUS_EXPLORER_UUID)
class BusExplorer(MemoryMappedInstrument):
    """One target bus, explored one operation at a time."""

    # The one child of the envelope, at offset 0.
    BLOCK = "engine"

    OP_READ = 0
    OP_WRITE = 1
    OP_MASKED_WRITE = 2
    # What an operation is called in the journal, on a pill and in an error.
    OP_NAMES = {OP_READ: "read", OP_WRITE: "write",
                OP_MASKED_WRITE: "masked-write"}

    ERROR_OK = 0
    ERROR_NAMES = {0: "ok", 1: "timeout", 2: "slverr", 3: "reserved-command"}

    STATUS_BUSY = 0
    STATUS_DONE = 1
    STATUS_ERROR_LSB = 2
    STATUS_SCAN_ACTIVE = 4

    STATE_IDLE = "idle"
    STATE_BUSY = "busy"
    STATE_SCANNING = "scanning"
    STATE_ERROR = "error"

    # Status reads a fire-then-poll may take before the driver gives up on the
    # engine itself. The engine completes every command it accepts, timeout
    # included, so more than a couple means the register file is not answering
    # what the descriptor says it is.
    POLL_LIMIT = 1000
    # Seconds between status reads once the first one found the engine busy.
    POLL_INTERVAL = 0.001

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        # [ address-width, data-width, slot-count, map identifier ]
        (self.address_width, self.data_width, self.slot_count,
         self.map_id) = self.tail
        self.last_fingerprint = None
        self.journal = Journal(envelope.name,
                               address_digits=(self.address_width + 3) // 4,
                               value_digits=(self.data_width + 3) // 4)
        self.library = MapLibrary()
        self.map = None
        self.map_source = None
        # Why the descriptor's map identifier resolved to nothing, for the
        # user to read. Not an error: raw hex is a working mode.
        self.map_error = None
        # Slot addresses are host state the gateware only stores; caching them
        # keeps the status poll to the one burst read it is meant to be.
        self.slots = [None] * self.slot_count
        self.slot_enable = 0
        self.scan_enabled = False
        self.snapshots = {}
        self.__engine = None
        self.__load_declared_map()

    def siblings_resolve(self, siblings):
        """Bind the engine. Called by the enumerator once every child of the
        instrument exists."""
        child = siblings.get(self.BLOCK)
        if child is None:
            raise LookupError(
                f"bus explorer {self.name!r} has no {self.BLOCK!r} child "
                f"(children: {', '.join(sorted(siblings))})")
        if not isinstance(child, BusExplorerEngine):
            raise TypeError(
                f"bus explorer {self.name!r} holds {self.BLOCK!r} as a "
                f"{type(child).__name__}, not its engine register file")
        self.__engine = child

    @property
    def engine(self):
        assert self.__engine is not None, (
            f"bus explorer {self.name!r} was never resolved against its "
            f"children")
        return self.__engine

    # -- geometry ----------------------------------------------------------

    def bus_bytes(self):
        """Bytes of the target APB data bus: a declared data width that is not
        one of the three the bus comes in rides the next one up."""
        for width in (8, 16, 32):
            if self.data_width <= width:
                return width // 8
        raise AssertionError(f"data width {self.data_width} exceeds 32")

    def address_mask(self):
        return (1 << self.address_width) - 1

    def data_mask(self):
        return (1 << self.data_width) - 1

    def check_address(self, address):
        if not isinstance(address, int) or address < 0 \
                or address > self.address_mask():
            raise ValueError(
                f"target address {address!r} does not fit the "
                f"{self.address_width} address bit(s) of explorer "
                f"{self.name!r} (0 to {self.address_mask():#x})")
        return address

    def check_data(self, value, what="value"):
        if not isinstance(value, int) or value < 0 or value > self.data_mask():
            raise ValueError(
                f"{what} {value!r} does not fit the {self.data_width} data "
                f"bit(s) of explorer {self.name!r} "
                f"(0 to {self.data_mask():#x})")
        return value

    # -- the register map ---------------------------------------------------

    def __load_declared_map(self):
        """Resolve the descriptor's map identifier against the user's library,
        once, at enumeration. A missing or unreadable document is remembered
        and not raised: an explorer with no map still explores."""
        if not self.map_id:
            return
        try:
            document = self.library.resolve(self.map_id)
        except (SvdError, OSError) as e:
            self.map_error = str(e)
            return
        if document is None:
            self.map_error = (
                f"no SVD document is registered as {self.map_id!r} "
                f"(acrobe gatecap bus map add {self.map_id} <file.svd>)")
            return
        self.map, self.map_source = document, self.library.path(self.map_id)

    def map_file(self, path):
        """Use a document straight off disk, whatever the descriptor says. What
        a file picker in the pane and a --map on the command line both do."""
        self.map = SvdDocument.parse_file(path)
        self.map_source, self.map_error = str(path), None
        return self.map

    def map_use(self, map_id):
        """Use the document registered under an identifier."""
        document = self.library.resolve(map_id)
        if document is None:
            raise KeyError(f"no SVD document is registered as {map_id!r}")
        self.map, self.map_source = document, self.library.path(map_id)
        self.map_error = None
        return document

    def register_at(self, address):
        """The register the map puts at a target address, or None."""
        return None if self.map is None else self.map.register_at(address)

    def name_at(self, address):
        register = self.register_at(address)
        return None if register is None else register.qualified

    def fields_at(self, address, value):
        """The field breakdown of a value read at an address, or [] with no
        map (or a register the map gives no fields)."""
        register = self.register_at(address)
        return [] if register is None else register.decode(value)

    def register(self, name):
        """A register by name. Refused with no map: there is nothing to look
        the name up in, and guessing an address is not on offer."""
        if self.map is None:
            raise LookupError(
                f"explorer {self.name!r} has no register map loaded, so "
                f"{name!r} names nothing; address the target in hex, or "
                f"register a map for {self.map_id or 'this target'}")
        return self.map.register(name)

    # -- one operation ------------------------------------------------------

    async def __access(self, op, address, wdata=0, wmask=0,
                       register=None, field=None):
        """The one path every target access takes: stage, fire, poll, decode,
        journal. Returns the value the engine reported (a read's data, and what
        a masked write read before modifying it); raises on an error code."""
        code, rdata = await self.__transact(op, address, wdata, wmask)
        error = None if code == self.ERROR_OK else self.ERROR_NAMES[code]
        self.journal.observe(op=self.OP_NAMES[op], address=address,
                             value=wdata, mask=None if op != self.OP_MASKED_WRITE
                             else wmask,
                             register=(register if register is not None
                                       else self.name_at(address)),
                             field=field, error=error)
        if code != self.ERROR_OK:
            raise BusAccessError.of(code, self.OP_NAMES[op], address)
        return rdata

    async def __transact(self, op, address, wdata, wmask):
        """Fire one staged operation and poll it to completion. ``(error code,
        RDATA)``."""
        await self.engine.fire(address, wdata, wmask, op)
        for attempt in range(self.POLL_LIMIT):
            words = await self.engine.words(self.engine.STATUS, 3)
            status = words[0]
            busy = (status >> self.STATUS_BUSY) & 1
            done = (status >> self.STATUS_DONE) & 1
            if done and not busy:
                code = (status >> self.STATUS_ERROR_LSB) & 3
                return code, words[2]
            if attempt:
                await asyncio.sleep(self.POLL_INTERVAL)
        raise TimeoutError(
            f"explorer {self.name!r}: the engine reported neither done nor "
            f"idle after {self.POLL_LIMIT} status reads of a "
            f"{self.OP_NAMES[op]} of {address:#x}")

    async def read(self, address):
        """One target read. Raises :class:`BusAccessError` on a target that
        refused or never answered."""
        return await self.__access(self.OP_READ, self.check_address(address))

    async def write(self, address, value):
        """One target write, of the whole data word."""
        await self.__access(self.OP_WRITE, self.check_address(address),
                            wdata=self.check_data(value))

    async def write_masked(self, address, value, mask, field=None):
        """A read-modify-write on the target, executed by the engine as an
        indivisible pair with respect to the scanner. Returns the value the
        engine read before modifying it -- and, when the read failed, the write
        was not performed at all."""
        return await self.__access(
            self.OP_MASKED_WRITE, self.check_address(address),
            wdata=self.check_data(value), wmask=self.check_data(mask, "mask"),
            field=field)

    async def field_write(self, register_name, field_name, value):
        """Write one field of one mapped register: the map turns the pair into
        an address and a mask, and the gateware does the read-modify-write.
        ``value`` is an integer or one of the field's enumerated names."""
        register = self.register(register_name)
        field = register.field(field_name)
        if not register.writable():
            raise ValueError(
                f"register {register.qualified} is {register.access}")
        if not field.writable():
            raise ValueError(
                f"field {register.qualified}.{field.name} is {field.access}")
        placed = field.place(field.encode(value))
        await self.__access(self.OP_MASKED_WRITE, register.address,
                            wdata=self.check_data(placed),
                            wmask=self.check_data(field.mask, "mask"),
                            register=register.qualified, field=field.name)
        return {"address": register.address, "value": placed,
                "mask": field.mask}

    async def sweep(self, start, count, step=None):
        """Read a run of target addresses, one engine operation each. An
        address that errors is reported rather than raised: a sweep of an
        unknown map is expected to hit holes.

        ``step`` defaults to the width of the target data bus, which is what
        consecutive registers are spaced by on a byte-addressed target."""
        step = self.bus_bytes() if step is None else step
        if count < 0 or step <= 0:
            raise ValueError(f"a sweep of {count} address(es) by {step} reads "
                             f"nothing")
        out = []
        for index in range(count):
            address = self.check_address(start + index * step)
            entry = {"address": address, "value": None, "error": None,
                     "register": self.name_at(address)}
            try:
                entry["value"] = await self.read(address)
            except BusAccessError as e:
                entry["error"] = self.ERROR_NAMES[type(e).CODE]
            out.append(entry)
        return out

    # -- the scanner --------------------------------------------------------

    async def slots_set(self, addresses, enable=True):
        """Program the slot addresses, low slot first, and enable exactly
        those. Slots past the end of the list are left addressed as they were
        and disabled -- a disabled slot is read by nothing."""
        addresses = [self.check_address(a) for a in addresses]
        if len(addresses) > self.slot_count:
            raise ValueError(
                f"explorer {self.name!r} has {self.slot_count} scan slot(s), "
                f"not the {len(addresses)} addresses given")
        await self.engine.slot_addresses_write(addresses)
        for index, address in enumerate(addresses):
            self.slots[index] = address
        mask = (1 << len(addresses)) - 1 if enable else 0
        await self.slots_enable(mask)
        return list(self.slots)

    async def slots_read(self):
        """The slot addresses the gateware holds, and the enable mask beside
        them."""
        self.slots = await self.engine.slot_addresses(self.slot_count)
        self.slot_enable = await self.engine.word(self.engine.SLOT_ENABLE)
        return list(self.slots)

    async def slots_enable(self, mask):
        """The whole enable mask at once. Writing it clears the valid and error
        flags of the slots it disables."""
        if mask < 0 or mask >= (1 << self.slot_count):
            raise ValueError(
                f"enable mask {mask:#x} does not fit the {self.slot_count} "
                f"slot(s) of explorer {self.name!r}")
        await self.engine.write(self.engine.SLOT_ENABLE, mask)
        self.slot_enable = mask

    async def slot_set(self, index, address, enabled=True):
        """One slot: its address, then its enable bit."""
        self.__check_slot(index)
        await self.engine.write(
            self.engine.SLOT_ADDRESS + index * self.engine.word_bytes(),
            self.check_address(address))
        self.slots[index] = address
        await self.slot_enabled(index, enabled)

    async def slot_enabled(self, index, enabled):
        self.__check_slot(index)
        bit = 1 << index
        await self.slots_enable((self.slot_enable | bit) if enabled
                                else (self.slot_enable & ~bit))

    def __check_slot(self, index):
        if not 0 <= index < self.slot_count:
            raise IndexError(
                f"explorer {self.name!r} has slots 0 to {self.slot_count - 1}, "
                f"not {index}")

    async def scan(self, enabled=True):
        """Start or stop the round-robin sweep of the enabled slots."""
        await self.engine.write(self.engine.SCAN_CTRL, 1 if enabled else 0)
        self.scan_enabled = bool(enabled)

    async def scan_read(self):
        """One entry per slot, out of the same burst the poll reads: the
        address it was programmed with, its last value, and its flags. An
        erroring slot keeps its last good value and raises its error bit."""
        return self.__scan(await self.engine.status(self.slot_count))

    def __scan(self, words):
        valid, error, results = words[3], words[4], words[5:]
        return [{"index": index,
                 "address": self.slots[index],
                 "register": (None if self.slots[index] is None
                              else self.name_at(self.slots[index])),
                 "enabled": bool((self.slot_enable >> index) & 1),
                 "valid": bool((valid >> index) & 1),
                 "error": bool((error >> index) & 1),
                 "value": results[index]}
                for index in range(self.slot_count)]

    # -- snapshots ----------------------------------------------------------

    async def snapshot(self, name, start=None, count=None, step=None,
                       addresses=None):
        """Read a set of target addresses into a named snapshot: an explicit
        list, a range, or -- naming neither -- the programmed slots. A
        host-driven engine loop; nothing in the gateware knows about it."""
        if addresses is None and start is None:
            addresses = [a for a in self.slots if a is not None]
            if not addresses:
                raise ValueError(
                    f"explorer {self.name!r} has no slot addresses programmed, "
                    f"so a snapshot must name a range or a list of addresses")
        if addresses is not None:
            entries = []
            for address in addresses:
                entry = {"address": self.check_address(address),
                         "value": None, "error": None}
                try:
                    entry["value"] = await self.read(address)
                except BusAccessError as e:
                    entry["error"] = self.ERROR_NAMES[type(e).CODE]
                entries.append(entry)
        else:
            entries = await self.sweep(start, count, step)
        snap = Snapshot(name=name, time=time.time())
        for entry in entries:
            if entry["error"] is None:
                snap.values[entry["address"]] = entry["value"]
            else:
                snap.errors[entry["address"]] = entry["error"]
        self.snapshots[name] = snap
        return snap

    def diff(self, before, after):
        """What changed between two snapshots, address by address. A snapshot
        may be named or passed by value. Addresses only one of them holds are
        reported too, with the missing side as None -- two snapshots of
        different ranges is a user mistake worth seeing, not one to hide."""
        before, after = self.__snapshot(before), self.__snapshot(after)
        changes = []
        for address in sorted(set(before.addresses()) | set(after.addresses())):
            old = before.values.get(address)
            new = after.values.get(address)
            old_error = before.errors.get(address)
            new_error = after.errors.get(address)
            if old == new and old_error == new_error:
                continue
            register = self.register_at(address)
            changes.append({
                "address": address,
                "register": None if register is None else register.qualified,
                "before": old, "after": new,
                "before_error": old_error, "after_error": new_error,
                "fields": ([] if register is None or old is None or new is None
                           else [f["name"] for f in register.decode(old)
                                 if f["value"] != register.field(f["name"])
                                 .extract(new)])})
        return changes

    def __snapshot(self, which):
        if isinstance(which, Snapshot):
            return which
        snap = self.snapshots.get(which)
        if snap is None:
            raise KeyError(
                f"explorer {self.name!r} holds no snapshot named {which!r}"
                + (" (" + ", ".join(sorted(self.snapshots)) + ")"
                   if self.snapshots else ""))
        return snap

    # -- the journal --------------------------------------------------------

    def recipe(self):
        return self.journal.recipe(self.map_id)

    async def replay(self, recipe):
        """Execute a recipe against this target, step by step, in order. Every
        step is journalled like any other write -- replaying a session is a
        session too."""
        steps = Journal.steps_of(recipe)
        for index, step in enumerate(steps):
            mask = step.get("mask")
            try:
                if step["op"] == "masked-write":
                    if mask is None:
                        raise ValueError(
                            f"recipe step {index} is a masked write with no "
                            f"mask")
                    await self.write_masked(step["address"], step["value"],
                                            mask)
                else:
                    await self.write(step["address"], step["value"])
            except (BusAccessError, ValueError) as e:
                raise RuntimeError(
                    f"recipe step {index} ({step['op']} of "
                    f"{step['address']:#x}) failed: {e}") from e
        return len(steps)

    # -- the whole live state ----------------------------------------------

    async def fingerprint(self):
        """Per-instance descriptor UID, the same one every block of the rack
        reports."""
        self.last_fingerprint = await self.engine.word(
            self.engine.FINGERPRINT)
        return self.last_fingerprint

    async def status(self):
        """The whole read-only run in one burst: the engine's state, the
        fingerprint, the last read data and every slot."""
        words = await self.engine.status(self.slot_count)
        status = words[0]
        self.last_fingerprint = words[1]
        done = bool((status >> self.STATUS_DONE) & 1)
        code = (status >> self.STATUS_ERROR_LSB) & 3
        return {"fingerprint": words[1],
                "busy": bool((status >> self.STATUS_BUSY) & 1),
                "done": done,
                # The error code describes the last command that completed, so
                # it means nothing until one has.
                "error": self.ERROR_NAMES[code] if done else None,
                "scan_active": bool((status >> self.STATUS_SCAN_ACTIVE) & 1),
                "rdata": words[2],
                "scan": self.__scan(words)}

    async def poll(self):
        """This instrument's status for a frontend: the engine state, every
        scan slot, and the state and tone its pill is drawn from. A target that
        refused or never answered is what an explorer has to raise."""
        if self.slots.count(None) == self.slot_count:
            # Slot addresses are write-only host state the gateware stores;
            # read them once so the poll can name what each result is.
            await self.slots_read()
        state = await self.status()
        failed = [entry for entry in state["scan"] if entry["error"]]
        attention = state["error"] not in (None, "ok") or bool(failed)
        return dict(state,
                    slots=list(self.slots),
                    state=self.__state(state, attention),
                    tone=("attention" if attention
                          else "active" if state["busy"]
                          or state["scan_active"] else "idle"),
                    progress=self.__progress(state, failed))

    def __state(self, state, attention):
        if state["busy"]:
            return self.STATE_BUSY
        if attention:
            return self.STATE_ERROR
        if state["scan_active"]:
            return self.STATE_SCANNING
        return self.STATE_IDLE

    def __progress(self, state, failed):
        if failed:
            return "slot error: " + ", ".join(self.__slot_label(entry)
                                              for entry in failed)
        if state["error"] not in (None, "ok"):
            return f"last access: {state['error']}"
        if state["scan_active"]:
            live = [entry for entry in state["scan"] if entry["valid"]]
            return f"scanning {len(live)} slot(s)"
        return "idle"

    @staticmethod
    def __slot_label(entry):
        """How a slot is named in a pill: by the register the map found there,
        else by its address, else by its index."""
        if entry["register"] is not None:
            return entry["register"]
        if entry["address"] is not None:
            return f"{entry['address']:#x}"
        return f"slot {entry['index']}"

    # -- frontends ---------------------------------------------------------

    def ui_adaptor(self, frontend, resources=None):
        cached = self.__dict__.get(f"ui_{frontend}")
        if cached is not None:
            return cached
        if frontend == "gui":
            adaptor = BusExplorerGui(self, resources)
        elif frontend == "console":
            adaptor = BusExplorerConsole(self)
        else:
            return None
        self.__dict__[f"ui_{frontend}"] = adaptor
        return adaptor


class BusExplorerConsole(ConsoleAdaptor):
    """Console UI: the target's dimensions and what the host knows of its
    map."""

    CSV_HEADER = "address,register,value,error"

    def info(self):
        driver = self.driver
        lines = [f"{driver.name}:",
                 f"  bus explorer: {driver.address_width} address bit(s), "
                 f"{driver.data_width} data bit(s), "
                 f"{driver.slot_count} scan slot(s)"]
        if driver.map is not None:
            lines.append(f"  map {driver.map_id or '(file)'}: "
                         f"{len(driver.map)} register(s) from "
                         f"{driver.map_source}")
        elif driver.map_id:
            lines.append(f"  map {driver.map_id}: {driver.map_error}")
        else:
            lines.append("  no map identifier in the descriptor: raw hex")
        return lines

    @staticmethod
    def csv(entries):
        """A sweep as ``address,register,value,error`` text."""
        lines = [BusExplorerConsole.CSV_HEADER]
        for entry in entries:
            value = ("" if entry["value"] is None
                     else f"0x{entry['value']:x}")
            lines.append(f"0x{entry['address']:x},{entry['register'] or ''},"
                         f"{value},{entry['error'] or ''}")
        return "\n".join(lines) + "\n"


class BusExplorerGui(GuiAdaptor):
    """Web UI: raw access, the registers of interest the scanner keeps live,
    the field breakdown of a mapped register, and the journal."""

    PANEL = files(__package__).joinpath("panel.js")
    ORDER = 45   # below the capture panels, beside the other register panes

    def describe(self):
        driver = self.driver
        # The descriptor's facts and nothing else: the panel key is a hash of
        # this, and it must follow the gateware instrument, not the map a user
        # happens to have registered. The map itself comes over the "map" op.
        meta = {"name": self.address(), "type": str(BUS_EXPLORER_UUID),
                "address_width": driver.address_width,
                "data_width": driver.data_width,
                "slot_count": driver.slot_count,
                "map_id": driver.map_id,
                "bus_bytes": driver.bus_bytes()}
        meta["key"] = self.panel_key(meta)
        return meta

    async def message(self, msg):
        op, driver = msg.get("op"), self.driver
        if op == "map":
            return self.__map()
        if op == "map_use":
            driver.map_use(msg["id"])
            return self.__map()
        if op == "map_file":
            driver.map_file(msg["path"])
            return self.__map()
        if op == "read":
            return await self.__read(msg["address"])
        if op == "write":
            return await self.__write(msg)
        if op == "field":
            placed = await driver.field_write(msg["register"], msg["field"],
                                              msg["value"])
            return {"ok": True,
                    "summary": f"{msg['register']}.{msg['field']}="
                               f"{msg['value']}",
                    **placed}
        if op == "decode":
            return {"register": driver.name_at(msg["address"]),
                    "fields": driver.fields_at(msg["address"], msg["value"])}
        if op == "slots":
            await driver.slots_read()
            return {"slots": await driver.scan_read(),
                    "scan": driver.scan_enabled}
        if op == "slot_set":
            await driver.slot_set(msg["index"], msg["address"],
                                  msg.get("enabled", True))
            return {"ok": True,
                    "summary": f"slot {msg['index']} = "
                               f"{msg['address']:#x}"}
        if op == "slot_enable":
            await driver.slot_enabled(msg["index"], bool(msg["enabled"]))
            return {"ok": True}
        if op == "slots_set":
            await driver.slots_set([int(a) for a in msg["addresses"]])
            return {"ok": True, "summary": f"{len(msg['addresses'])} slot(s)"}
        if op == "scan":
            await driver.scan(bool(msg["enabled"]))
            return {"ok": True,
                    "summary": "scan " + ("on" if msg["enabled"] else "off")}
        if op == "journal":
            return {"entries": driver.journal.records(),
                    "text": driver.journal.listing()}
        if op == "journal_clear":
            return {"ok": True, "cleared": driver.journal.clear()}
        if op == "recipe":
            return {"recipe": driver.recipe(),
                    "text": driver.journal.recipe_text(driver.map_id)}
        if op == "replay":
            return {"ok": True, "steps": await driver.replay(msg["recipe"])}
        raise ValueError(f"unknown op {op!r}")

    def __map(self):
        driver = self.driver
        if driver.map is None:
            return {"loaded": False, "id": driver.map_id,
                    "error": driver.map_error, "registers": []}
        return {"loaded": True, "id": driver.map_id,
                "name": driver.map.name, "source": driver.map_source,
                "error": None,
                "registers": [
                    {"name": register.qualified, "short": register.name,
                     "address": register.address,
                     "access": register.access,
                     "description": register.description,
                     "writable": register.writable(),
                     "fields": register.decode(0)}
                    for register in sorted(driver.map.registers,
                                           key=lambda r: r.address)]}

    async def __read(self, address):
        driver = self.driver
        try:
            value = await driver.read(address)
        except BusAccessError as e:
            return {"error": str(e), "register": driver.name_at(address)}
        return {"value": value, "register": driver.name_at(address),
                "fields": driver.fields_at(address, value)}

    async def __write(self, msg):
        driver = self.driver
        address, value = msg["address"], msg["value"]
        mask = msg.get("mask")
        try:
            if mask is None or mask == driver.data_mask():
                await driver.write(address, value)
                summary = f"[{address:#x}] = {value:#x}"
            else:
                await driver.write_masked(address, value, mask)
                summary = f"[{address:#x}] = {value:#x} & {mask:#x}"
        except BusAccessError as e:
            return {"error": str(e)}
        name = driver.name_at(address)
        return {"ok": True,
                "summary": summary + (f" ({name})" if name else "")}
