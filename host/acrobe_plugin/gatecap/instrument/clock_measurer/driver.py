"""Clock-measurer instrument driver and its two UIs.

The instrument publishes one rate register per observed clock, plus the status
group every block carries. There is nothing to arm and nothing to configure:
the measurement is free-running, so the driver's whole surface is one read of
the contiguous rate array.

It holds its register file itself -- one instrument, one map, no child -- so
the node the rack enumerates is the register file: its base is the
instrument's own segment, and what it publishes comes out of the envelope's
tail.
"""

import uuid
from importlib.resources import files

from acrobe_plugin.gatecap.enumerator import (MemoryMappedEnumerator,
                                              MemoryMappedInstrument)
from acrobe_plugin.gatecap.frontend.adaptor import ConsoleAdaptor, GuiAdaptor

# Must match CLOCK_MEASURER_UUID_C in the gateware (gatecap.clock_measurer).
CLOCK_MEASURER_UUID = uuid.UUID("ba9af9d4-8767-4567-8e56-01bb12307fb7")


@MemoryMappedEnumerator.instruments.register(CLOCK_MEASURER_UUID)
class ClockMeasurer(MemoryMappedInstrument):
    """Rates of several clocks, measured against one reference clock."""

    # The register-map convention: the status group, then the rate array --
    # one word per clock, contiguous, in the descriptor's name order.
    REG_FINGERPRINT = 0x204
    REG_RATE = 0x300
    # A clock that has stopped reads as no edges at all, which is worth
    # raising to the user: it is the one thing a rate readout can report.
    STATE_MEASURING = "measuring"
    STATE_STOPPED = "stopped"

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        # [ reference-name, reference-hz, update-hz-l2, [ measured-names ] ]
        (self.reference_name, self.reference_hz, self.update_hz_l2,
         names) = self.tail
        self.clock_names = list(names)
        self.last_fingerprint = None

    def update_hz(self):
        """Times per second the rates refresh."""
        return 2**self.update_hz_l2

    def quantum_hz(self):
        """Resolution of a published rate. A measurement counts edges over one
        update period and scales the count back to Hz, so the rate it yields is
        a multiple of the number of update periods in a second -- the update
        rate is the resolution."""
        return self.update_hz()

    async def fingerprint(self):
        """Per-instance descriptor UID, the same one every block of the rack
        reports."""
        self.last_fingerprint = await self.bridge.read32(
            self.base + self.REG_FINGERPRINT)
        return self.last_fingerprint

    async def rates(self):
        """{clock name: measured rate in Hz} for every observed clock, from
        one burst read of the rate array."""
        wb = self.bridge.word_bytes
        raw = await self.bridge.mem_read(self.base + self.REG_RATE,
                                         len(self.clock_names) * wb)
        return {name: int.from_bytes(raw[i * wb:(i + 1) * wb], "little")
                for i, name in enumerate(self.clock_names)}

    async def poll(self):
        """This instrument's status for a frontend: every rate, plus the state
        and tone its pill is drawn from. The status group and the rate array
        sit in different regions of the map, so this is two reads -- the
        fingerprint, which every poll must carry, and the array."""
        fingerprint = await self.fingerprint()
        rates = await self.rates()
        stopped = [name for name, hz in rates.items() if hz == 0]
        return {"fingerprint": fingerprint, "rates": rates,
                "state": self.STATE_STOPPED if stopped
                         else self.STATE_MEASURING,
                "tone": "attention" if stopped else "active",
                "progress": ("not running: " + ", ".join(stopped) if stopped
                             else ", ".join(f"{name} {Rate.text(hz)}"
                                            for name, hz in rates.items()))}

    def ui_adaptor(self, frontend, resources=None):
        cached = self.__dict__.get(f"ui_{frontend}")
        if cached is not None:
            return cached
        if frontend == "gui":
            adaptor = ClockMeasurerGui(self, resources)
        elif frontend == "console":
            adaptor = ClockMeasurerConsole(self)
        else:
            return None
        self.__dict__[f"ui_{frontend}"] = adaptor
        return adaptor


class Rate:
    """Formatting shared by the console adaptor, the CSV dump and the pill."""

    UNITS = ((1_000_000_000, "GHz"), (1_000_000, "MHz"), (1_000, "kHz"))

    @classmethod
    def text(cls, hz):
        for scale, unit in cls.UNITS:
            if hz >= scale:
                return f"{hz / scale:.6g} {unit}"
        return f"{hz} Hz"


class ClockMeasurerConsole(ConsoleAdaptor):
    """Console UI: what the instrument watches, and the CSV the CLI dumps."""

    CSV_HEADER = "clock,rate_hz"

    def info(self):
        d = self.driver
        return [f"{d.name}:",
                f"  reference {d.reference_name}: "
                f"{Rate.text(d.reference_hz)} nominal",
                f"  measured clocks ({len(d.clock_names)}): "
                + ", ".join(d.clock_names),
                f"  refreshed {d.update_hz()} time(s) per second, to "
                f"{Rate.text(d.quantum_hz())}"]

    async def csv(self):
        """One read of every rate, as ``clock,rate_hz`` text."""
        rates = await self.driver.rates()
        lines = [self.CSV_HEADER]
        lines += [f"{name},{hz}" for name, hz in rates.items()]
        return "\n".join(lines) + "\n"


class ClockMeasurerGui(GuiAdaptor):
    """Web UI: a table of the current rates and a rolling history graph, both
    fed by the status poll -- the rates are the instrument's whole state, so
    the poll that draws its pill is the same read the pane renders."""

    PANEL = files(__package__).joinpath("panel.js")
    ORDER = 40   # below the capture panels, above the wider control/status one

    def describe(self):
        d = self.driver
        meta = {"name": self.address(), "type": str(CLOCK_MEASURER_UUID),
                "reference_name": d.reference_name,
                "reference_hz": d.reference_hz,
                "update_hz": d.update_hz(),
                "quantum_hz": d.quantum_hz(),
                "clock_names": d.clock_names}
        meta["key"] = self.panel_key(meta)
        return meta

    async def message(self, msg):
        if msg.get("op") == "read":
            return {"rates": await self.driver.rates()}
        raise ValueError(f"unknown op {msg.get('op')!r}")
