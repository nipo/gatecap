"""The control/status instrument: a generic front panel of plain wires.

Importing this package registers everything the instrument contributes: the
``!control-status`` tag on the generator (:mod:`.generator`), which emits the
register shell, the event core and the crossings between them out of a
description naming controls, statuses and ticks, and the instrument driver
with its register file and its pane on the discovery layer (:mod:`.driver`,
:mod:`.blocks`).

The panel a host shows is a pure function of the inventory the envelope tail
carries (:mod:`.inventory`): kind, width and enumeration binding decide every
widget, so nothing about a panel is known here in advance.

The instrument is registered exactly the way a third-party one is -- by being
imported, and calling the registries from its own package.
"""

from . import blocks     # noqa: F401 -- registers the register-file driver
from .driver import CONTROL_STATUS_UUID, ControlStatusPanel
from .generator import ControlStatus, Panel
from .inventory import PanelInventory, PanelMap

__all__ = ["CONTROL_STATUS_UUID", "ControlStatus", "ControlStatusPanel",
           "Panel", "PanelInventory", "PanelMap"]
