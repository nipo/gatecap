"""The journal of a bring-up session: every write the host made to the target.

Peek-poke work ends with the question "which writes got me here", and the
answer is the artifact the session exists to produce. Every target access the
driver performs passes through :meth:`Journal.observe`; the writes among them
are kept, with the register and field names the map decoded them to at the
moment they were made, and the outcome the engine reported.

Two exports, from the same entries: a listing a human reads, and a recipe the
node replays. The recipe is deliberately name-free where it matters -- a step
carries the address, the value and the mask that were actually driven, so it
replays identically against a host with no map registered, and the decoded
names ride along only as commentary.

The journal is session-scoped. Nothing here writes to disk: what a session is
worth keeping is what the user exported from it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class JournalEntry:
    """One write, as it was made."""

    time: float
    op: str
    address: int
    value: int
    mask: int | None
    register: str | None
    field: str | None
    error: str | None

    def stamp(self):
        return time.strftime("%H:%M:%S", time.localtime(self.time)) \
            + f".{int(self.time * 1000) % 1000:03d}"

    def decoded(self):
        """How the map named this write, or None with no map."""
        if self.register is None:
            return None
        if self.field is None:
            return self.register
        return f"{self.register}.{self.field}"

    def text(self, address_digits=8, value_digits=8):
        """One line of the plain listing."""
        parts = [self.stamp(), f"{self.op:<12}",
                 f"[{self.address:0{address_digits}x}]",
                 f"= {self.value:0{value_digits}x}"]
        if self.mask is not None:
            parts.append(f"mask {self.mask:0{value_digits}x}")
        decoded = self.decoded()
        if decoded is not None:
            parts.append(decoded)
        if self.error is not None:
            parts.append(f"!! {self.error}")
        return "  ".join(parts)

    def step(self):
        """This entry as a recipe step: what to drive, not what it was
        called."""
        step = {"op": self.op, "address": self.address, "value": self.value}
        if self.mask is not None:
            step["mask"] = self.mask
        decoded = self.decoded()
        if decoded is not None:
            step["name"] = decoded
        return step


class Journal:
    """Every write of one session, in order."""

    # The operations worth recording. A read changes nothing and would drown
    # the record it is meant to be, so it passes through and is only counted.
    WRITES = ("write", "masked-write")
    VERSION = 1

    def __init__(self, instrument, address_digits=8, value_digits=8):
        self.instrument = instrument
        self.address_digits = address_digits
        self.value_digits = value_digits
        self.entries = []
        self.reads = 0

    def __len__(self):
        return len(self.entries)

    def observe(self, op, address, value=0, mask=None, register=None,
                field=None, error=None):
        """The one hook every target access the node performs goes through.
        Returns the entry it kept, or None for an access that is not a
        write."""
        if op not in self.WRITES:
            self.reads += 1
            return None
        entry = JournalEntry(time=time.time(), op=op, address=address,
                             value=value, mask=mask, register=register,
                             field=field, error=error)
        self.entries.append(entry)
        return entry

    def clear(self):
        count, self.entries, self.reads = len(self.entries), [], 0
        return count

    def records(self):
        """The entries as plain dicts, for a frontend."""
        return [dict(asdict(entry), decoded=entry.decoded(),
                     stamp=entry.stamp()) for entry in self.entries]

    def listing(self):
        """The plain text listing: one line per write, in order."""
        header = (f"# gatecap bus explorer {self.instrument}: "
                  f"{len(self.entries)} write(s), {self.reads} read(s)")
        return "\n".join(
            [header] + [entry.text(self.address_digits, self.value_digits)
                        for entry in self.entries]) + "\n"

    def recipe(self, map_id=None):
        """The replayable recipe: the steps, in order, as they were driven."""
        return {"gatecap-bus-explorer-recipe": self.VERSION,
                "instrument": self.instrument,
                "map": map_id or None,
                "steps": [entry.step() for entry in self.entries]}

    def recipe_text(self, map_id=None):
        return json.dumps(self.recipe(map_id), indent=2) + "\n"

    @classmethod
    def steps_of(cls, recipe):
        """The steps of a recipe, checked. Accepts the document
        :meth:`recipe` emits, or a bare list of steps."""
        if isinstance(recipe, str):
            recipe = json.loads(recipe)
        if isinstance(recipe, list):
            steps = recipe
        elif isinstance(recipe, dict):
            version = recipe.get("gatecap-bus-explorer-recipe")
            if version is None:
                raise ValueError(
                    "this is not a bus-explorer recipe: it carries no "
                    "gatecap-bus-explorer-recipe version")
            if version != cls.VERSION:
                raise ValueError(
                    f"recipe version {version} is not the {cls.VERSION} this "
                    f"host writes and replays")
            steps = recipe.get("steps", [])
        else:
            raise ValueError(f"a recipe is a document or a list of steps, "
                             f"not {type(recipe).__name__}")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"recipe step {index} is not an object")
            if step.get("op") not in cls.WRITES:
                raise ValueError(
                    f"recipe step {index} has op {step.get('op')!r}, which is "
                    f"not one of {', '.join(cls.WRITES)}")
            for key in ("address", "value"):
                if not isinstance(step.get(key), int):
                    raise ValueError(
                        f"recipe step {index} has no integer {key}")
        return list(steps)
