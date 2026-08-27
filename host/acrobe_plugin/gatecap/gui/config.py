"""User-local config: recent roots, per-panel settings, and whatever an
instrument keeps between sessions (a bus explorer's SVD map bindings).

Stored as JSON in the platform's per-user config directory so it survives
across sessions and webview instances. ``GATECAP_CONFIG_DIR`` overrides that
directory.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def config_dir():
    # An explicit directory wins over the platform's: a test, or a user
    # keeping one config per bench, says where the store is instead of
    # writing into the account's real one.
    override = os.environ.get("GATECAP_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gatecap"


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "config.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)  # atomic

    def recent(self):
        return self.data.get("recent", [])

    def add_recent(self, root, limit=8):
        rest = [r for r in self.recent() if r != root]
        self.data["recent"] = ([root] + rest)[:limit]
        self.save()
        return self.data["recent"]

    # Per-panel settings, keyed by a hash of the instrument's description (so
    # they follow the gateware, not the transport root).
    def panel_settings(self, key):
        return self.data.get("panels", {}).get(key, {})

    def set_panel_settings(self, key, settings):
        self.data.setdefault("panels", {})[key] = settings
        self.save()
