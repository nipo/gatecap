"""The bus-explorer instrument: an APB master the host drives.

Interactive exploration of a register map that lives outside the rack -- a
transceiver DRP port, a PLL reconfiguration interface, third-party IP. There is
no pass-through aperture: every target access goes through an indirect command
engine that owns a timeout, so a target that never answers costs a timeout
instead of wedging the transport. Beside the engine, a scanner sweeps the slots
the host programmed, keeping the registers of interest live in the standard
burst status poll.

Importing this package registers what the instrument contributes: the
``!bus-explorer`` tag on the generator (:mod:`.generator`), which emits the
register shell, the bus core and the stream crossings between them out of a
description naming the target's dimensions; the instrument driver with its
engine register file and its pane on the discovery layer (:mod:`.driver`,
:mod:`.blocks`); and the ``bus`` command group on ``acrobe gatecap``
(:mod:`.cli`).

Register-map decode is host-side alone (:mod:`.svd`): the descriptor names a
map, the host resolves that name against the SVD documents the user registered,
and everything degrades to raw hex when it has none. What the session wrote is
kept in the journal (:mod:`.journal`), which is the artifact bring-up exists to
produce.

The instrument is registered exactly the way a third-party one is -- by being
imported, and calling the registries from its own package.
"""

from . import blocks     # noqa: F401 -- registers the engine driver
from . import cli        # noqa: F401 -- adds `acrobe gatecap bus`
from . import generator  # noqa: F401 -- registers the !bus-explorer tag
from .driver import (BUS_EXPLORER_UUID, BusAccessError, BusCommandError,
                     BusExplorer, BusSlaveError, BusTimeout, Snapshot)
from .generator import Explorer
from .journal import Journal, JournalEntry
from .svd import MapLibrary, SvdDocument, SvdError

__all__ = ["BUS_EXPLORER_UUID", "BusAccessError", "BusCommandError",
           "BusExplorer", "BusSlaveError", "BusTimeout", "Explorer",
           "Journal", "JournalEntry", "MapLibrary", "Snapshot", "SvdDocument",
           "SvdError"]
