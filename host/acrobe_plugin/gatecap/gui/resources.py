"""Runtime resource serving, addressed by owner identity.

In a live process the natural stable address for a served resource is the
``id()`` of the object that owns it: a class shared across all its instances
(a panel, the Surfer bundle) collapses to one URL, and a per-instance owner (a
block's trace) gets its own. A per-run token in the path makes a process
restart bust the browser cache, so shared resources are served ``immutable``
and fetched once no matter how many panes reference them.

An owner is any object exposing ``resource(name) -> (bytes, content_type)``.
"""

from __future__ import annotations

import re
import secrets

MIME = {".wasm": "application/wasm", ".js": "text/javascript",
        ".json": "application/json", ".html": "text/html",
        ".vcd": "text/plain", ".css": "text/css"}

IMMUTABLE = "public, max-age=31536000, immutable"


class ResourceServer:
    """Maps ``/r/<run>/<owner-id>/<name>`` to the owning object's resource."""

    def __init__(self):
        self.run = secrets.token_hex(6)
        self.owners = {}            # id(owner) -> owner (strong ref: id stays reserved)
        self.immutable = set()      # (id(owner), name) served immutable

    def mint(self, owner, name, immutable=False):
        """Register owner and return the URL its resource is served at. Minting
        again with the same (owner, name) yields the same URL (idempotent)."""
        key = id(owner)
        self.owners[key] = owner
        if immutable:
            self.immutable.add((key, name))
        return f"/r/{self.run}/{key}/{name}"

    def serve(self, run, key, name):
        """(bytes, content_type, cache_control) for a request, or None."""
        if run != self.run:
            return None
        owner = self.owners.get(key)
        if owner is None:
            return None
        got = owner.resource(name)
        if got is None:
            return None
        body, ctype = got
        cache = IMMUTABLE if (key, name) in self.immutable else "no-store"
        return body, ctype, cache


class WaveformSurface:
    """The shared Surfer runtime. Owns the prebuilt assets and, addressed by
    this object's id, serves them once to every waveform pane's iframe. Rewrites
    Surfer's index so its asset references point at the registry (the heavy
    wasm/js are then fetched a single time and cached immutable)."""

    ASSETS = ("surfer.js", "surfer_bg.wasm", "integration.js", "manifest.json")

    def __init__(self, assets, resources):
        self.files = {}
        index = (assets / "index.html").read_text()
        index = re.sub(r'\s+integrity="[^"]*"', "", index)  # drop SRI (URL is the id)
        # Surfer's index only puts a few of the wasm exports on window; expose
        # the ones the shell needs too. Same module URL, so this shares the
        # initialized instance; references are grabbed eagerly but only called
        # once inject_message (assigned after init) has appeared.
        index = index.replace("</head>", (
            '<script type="module">'
            "import * as s from '/surfer.js';"
            "window.waves_loaded = s.waves_loaded;"
            "window.get_state = s.get_state;"
            "</script></head>"))
        for name in self.ASSETS:
            f = assets / name
            if not f.is_file():
                continue
            self.files[name] = (f.read_bytes(),
                                MIME.get(f.suffix, "application/octet-stream"))
            url = resources.mint(self, name, immutable=True)
            # Surfer references these both as "/name" (absolute, via base href)
            # and bare "name"; point both at the registry URL.
            index = index.replace(f"/{name}", url).replace(f'"{name}"', f'"{url}"')
        self.files["index.html"] = (index.encode(), "text/html")
        # #dev disables Surfer's service worker so our immutable caching is the
        # only cache in play.
        self.url = resources.mint(self, "index.html", immutable=True) + "#dev"

    def resource(self, name):
        return self.files.get(name)
