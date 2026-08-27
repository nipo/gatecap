"""The seam between a frontend-free driver node and a concrete UI.

A driver exposes ``ui_adaptor(frontend)`` (``"gui"`` or ``"console"``); the
framework talks only to the adaptor it returns, never to the driver directly.
This keeps drivers UI-agnostic and lets a third-party instrument ship its own
UI.

The GUI seam is an instrument's alone: one instrument, one panel, one top-bar
toggle, one status pill. Blocks are the register files an instrument is built
from, and whatever they offer the user is a section of their instrument's
panel, driven through the instrument's own adaptor -- two surfaces over one
piece of hardware could only be a way to disagree with each other. The console
seam is per node: ``info`` describes an instrument and each of its blocks in
turn, which is a description, not a control surface.
"""

from __future__ import annotations

import hashlib
import json


class UiAdaptor:
    """Bridges one driver node to one frontend."""

    def __init__(self, driver):
        self.driver = driver

    def address(self):
        """How a frontend names this node: an instrument by its instance name,
        a block below one qualified by the instrument holding it, so blocks of
        two instruments never share an address."""
        from ..enumerator import BlockAddress
        return BlockAddress.of(self.driver)


class GuiAdaptor(UiAdaptor):
    """An instrument's web UI. The framework serves ``resource(name)`` under
    the instrument's per-instance URL and routes UI messages to
    ``message(msg)``; the instrument owns its panel and any assets it needs."""

    ORDER = 100   # panels render top-to-bottom by ascending ORDER
    # The panel script, as an importlib.resources traversable. The default
    # resource() serves it as panel.js.
    PANEL = None

    def __init__(self, driver, resources):
        super().__init__(driver)
        self.resources = resources

    def describe(self):
        """This instrument's self-description for the manifest: a
        JSON-serialisable dict the shell binds a panel to. Must carry at least
        ``name``, ``type`` (the type UUID the shell renders by) and ``key``."""
        raise NotImplementedError

    @staticmethod
    def panel_key(meta):
        """A stable per-panel key (hash of the instrument's description) so
        saved settings follow the gateware instrument, not the transport root."""
        return hashlib.sha1(
            json.dumps(meta, sort_keys=True).encode()).hexdigest()[:16]

    def panel_url(self):
        """The URL the shell fetches the panel script from. Keyed by this
        adaptor's id, it is served immutable and refetched only when the
        instrument re-enumerates."""
        return self.resources.mint(self, "panel.js", immutable=True)

    def resource(self, name):
        """(bytes, content-type) for a named resource served under the
        instrument's URL namespace (panel.js, trace.vcd, images, ...), or None
        for 404."""
        if name == "panel.js" and self.PANEL is not None:
            return self.PANEL.read_bytes(), "text/javascript"
        return None

    async def message(self, msg):
        """Handle a message from this instrument's panel; return a JSON value."""
        raise NotImplementedError


class ConsoleAdaptor(UiAdaptor):
    """A driver's console UI. The CLI enumerates the nodes and asks each for
    its ``info()`` lines; a capture block also runs captures and renders the
    trace to a file format. Frontend-agnostic driver ops are shared with the
    GUI."""

    def info(self):
        """Human-readable description lines for this node, or []."""
        return []
