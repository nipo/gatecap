#!/usr/bin/env python3.13
"""gatecap GUI framework: an HTTP server hosting driver-provided panels.

The framework is a thin router. It opens a transport root, enumerates the
rack, and lets each instrument's GUI adaptor supply its own panel and serve its
own resources. One instrument is one panel, one top-bar toggle and one status
pill; the blocks an instrument is built from never reach the shell. Every
served resource is addressed by the runtime id() of its owner (see
:mod:`.resources`): the server mints the URLs and hands them to the shell
(connect returns a per-instrument ``panel_url`` and the shared ``surfer_url``;
a read returns a ``trace_url``). The shell registers each panel in a UUID-keyed
registry and routes UI messages back to the driver through
``instrument_message``. Surfer's prebuilt WASM build is a shared surface,
fetched once and embedded per panel that shows traces.

Everything, the API included, rides HTTP: the shell calls ``/api/<method>``
(JSON body = argument list, JSON response) whichever way it is displayed.
One asyncio loop owns it all -- the aiohttp server and the drivers' sockets
alike -- so a handler awaits the driver directly; requests overlap on the
loop, a slow trace read never starves the status polls. Two entry points
share the stack: :func:`main` opens a pywebview window (the window needs the
main thread, so the loop runs on a daemon thread), :func:`serve` runs on the
caller's loop with the browser left to the user. The server binds loopback
unless told otherwise -- the API is unauthenticated and drives hardware, so
reaching it across machines is an SSH tunnel's job by default.

Run:  python3.13 -m acrobe_plugin.gatecap.gui.app
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from aiohttp import WSMsgType, web

try:
    import webview
except ImportError:
    webview = None

from ..session import Session
from .config import Config
from .resources import ResourceServer, WaveformSurface
from .surfer_assets import ensure

HERE = Path(__file__).resolve().parent
SHELL = HERE / "assets" / "shell.html"
ICON = HERE / "assets" / "gatecap-icon.png"

POLL_PERIOD = 0.6   # seconds between hardware status polls per subscription


class Api:
    """Exposed to the shell as POST /api/<name>. Framework-level calls
    (connect/poll/settings/recent) plus the generic instrument_message router
    that forwards a panel's message to its instrument's adaptor. Driver-touching
    methods are async and run on the serving loop, where the driver's sockets
    live."""

    # The methods the HTTP route dispatches to; anything else 404s.
    EXPOSED = ("autoconnect_root", "recent_get", "recent_add", "settings_get",
               "settings_set", "connect", "disconnect", "info", "poll",
               "instrument_message")

    def __init__(self, resources):
        self.resources = resources
        self.session = None
        self.config = Config()
        self.surfer_url = None   # set once the shared Surfer surface is built
        self.autoconnect = None  # a root to connect to on load, or None
        # Last "configure" message per instrument. An analyzer's trigger
        # compare lives in hardware registers written once, when the user
        # edits it -- a device reset (re-enumeration) wipes them, so they are
        # replayed after a reconnect. Without this, a re-armed capture triggers
        # instantly on the reset-default (mask 0 = match any). Cleared per
        # connection.
        self.__instrument_config = {}

    def autoconnect_root(self):
        return self.autoconnect

    # -- framework services ------------------------------------------------

    def recent_get(self):
        return self.config.recent()

    def recent_add(self, root):
        return self.config.add_recent(root)

    def settings_get(self, key):
        return self.config.panel_settings(key)

    def settings_set(self, key, settings):
        self.config.set_panel_settings(key, settings)
        return {"ok": True}

    async def connect(self, root):
        self.__instrument_config = {}   # a new connection caches nothing yet
        try:
            self.session = Session(root)
            fingerprint = await self.session.open()
            return {"describe": self.__manifest(fingerprint),
                    "surfer_url": self.surfer_url}
        except Exception as e:
            return {"error": str(e)}

    def __manifest(self, fingerprint):
        """Collect each instrument's self-description + its id()-addressed
        panel URL (the client cannot compute id()), ordered top-to-bottom.
        Each instrument describes itself; the framework only gathers and
        orders. Its blocks are its own business: they hold no panel, and what
        they offer the user is a section of the instrument's."""
        instruments = []
        for node in self.session.instruments():
            factory = getattr(node, "ui_adaptor", None)
            adaptor = factory("gui", self.resources) if factory else None
            if adaptor is None:
                continue
            meta = adaptor.describe()
            meta["panel_url"] = adaptor.panel_url()
            meta["order"] = adaptor.ORDER
            instruments.append(meta)
        instruments.sort(key=lambda i: i["order"])
        return {"root": self.session.root, "fingerprint": fingerprint,
                "instruments": instruments}

    async def disconnect(self):
        try:
            if self.session is not None:
                await self.session.close()
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}
        finally:
            self.session = None
            self.__instrument_config = {}

    def info(self):
        """The console-adaptor description lines of every enumerated node --
        the same text as `gatecap info` -- for the log pane on
        connect/reconnect. Pure descriptor reads (no hardware round-trips)."""
        if self.session is None:
            return []
        lines = []
        for node in self.session.blocks():
            factory = getattr(node, "ui_adaptor", None)
            adaptor = factory("console") if factory else None
            if adaptor is not None:
                lines.extend(adaptor.info())
        return lines

    async def __read_status(self, name):
        # Any instrument that answers a status poll. What it reports is its own
        # -- the state name and tone it shows, and whatever else its panel
        # reads (progress, a trigger flag, a readback snapshot); the framework
        # only adds the rack-level fields below. The one field it must carry is
        # the fingerprint: the rack's change detection rides on every poll.
        instrument = self.session.instrument_by_name(name)
        # One driver call, timed for the RTT readout; the poll completing IS
        # the health signal, and its fingerprint doubles as the
        # instance-changed check, so no rack-level poll of its own is needed.
        t0 = time.perf_counter()
        p = await instrument.poll()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        # A trace readback in flight is answered from host memory (the fetch
        # owns the link), so that poll measured no round trip and reports none.
        fetch = p.get("fetch")
        return {**p,
                "changed": (self.session.fingerprint is not None
                            and p["fingerprint"] != self.session.fingerprint),
                "health": True,
                "latency_ms": None if fetch else latency_ms}

    async def poll(self, name):
        if self.session is None:
            return {"error": "not connected", "health": False}
        try:
            self.session.instrument_by_name(name)
        except KeyError as e:
            # Not a transport failure: nothing to rebuild, and no reason to.
            return {"error": str(e), "health": False}
        try:
            return await self.__read_status(name)
        except Exception as e:
            # The transport dropped -- the target re-enumerated, was
            # reprogrammed, or the cable glitched. Rebuild it in place: if the
            # board is back this reopens the handle and we return fresh status
            # tagged `reconnected` (the shell logs it and re-dumps info); if it
            # is still gone the rebuild raises and we report `reconnecting` (a
            # non-fatal wait). The shell keeps polling, so recovery is
            # automatic on a later tick.
            try:
                await self.session.reconnect()
            except Exception:
                return {"error": str(e), "health": False, "reconnecting": True}
            status = await self.__read_status(name)
            # Restore the register state (trigger compare) the reset wiped,
            # unless the gateware itself changed -- then the cached config no
            # longer applies and the shell goes stale on `changed`.
            if not status.get("changed"):
                status["replayed"] = await self.__replay_config()
            status["reconnected"] = True
            return status

    async def __replay_config(self):
        """Re-send each instrument's last "configure" to its fresh driver node,
        so the hardware matches the UI again after a reconnect. Returns a
        ``[{instrument, summary}]`` list for the log. Best effort: an
        instrument that no longer resolves or rejects the message is skipped
        (the next arm rewrites the capture's own registers regardless)."""
        replayed = []
        for name, msg in self.__instrument_config.items():
            adaptor = self.resolve_instrument(name)
            if adaptor is None:
                continue
            try:
                result = await adaptor.message(msg)
                replayed.append(
                    {"instrument": name,
                     "summary": result.get("summary")
                     if isinstance(result, dict) else None})
            except Exception:
                pass
        return replayed

    # -- instrument routing ------------------------------------------------

    def resolve_instrument(self, name):
        """The GUI adaptor for the named instrument, or None. Cached on the
        driver node and bound to the shared resource server, so URLs it mints
        resolve through the same registry the handler serves from."""
        if self.session is None or self.session.node is None:
            return None
        try:
            instrument = self.session.instrument_by_name(name)
        except KeyError:
            return None
        factory = getattr(instrument, "ui_adaptor", None)
        return factory("gui", self.resources) if factory else None

    async def instrument_message(self, name, msg):
        """Route a panel message to its instrument's adaptor. A successful
        "configure" (persistent register state, e.g. an analyzer's trigger
        compare) is cached so it can be replayed after a reconnect."""
        try:
            adaptor = self.resolve_instrument(name)
            if adaptor is None:
                return {"error": f"no instrument {name!r}"}
            result = await adaptor.message(msg)
            if isinstance(msg, dict) and msg.get("op") == "configure":
                self.__instrument_config[name] = msg
            return result
        except Exception as e:
            return {"error": str(e)}


def make_app(api, resources):
    shell = SHELL.read_bytes()

    async def shell_page(request):
        return web.Response(body=shell, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def resource(request):
        # An id()-addressed resource: /r/<run>/<id>/<name>.
        m = request.match_info
        got = resources.serve(m["run"], int(m["id"]), m["name"])
        if got is None:
            raise web.HTTPNotFound
        body, ctype, cache = got
        return web.Response(body=body, content_type=ctype,
                            headers={"Cache-Control": cache})

    async def api_call(request):
        # The whole JS->Python API: /api/<method>, JSON body = the positional
        # argument list, JSON response. Awaited right here on the serving
        # loop, under a backstop timeout so a wedged driver call surfaces as
        # an error instead of a request that never answers.
        name = request.match_info["method"]
        if api is None or name not in Api.EXPOSED:
            raise web.HTTPNotFound
        text = await request.text()
        try:
            args = json.loads(text) if text else []
            result = getattr(api, name)(*args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=60)
        except Exception as e:
            # The shell's fetch proxy turns a non-200 into a rejected
            # promise, the same failure shape a raising call has.
            raise web.HTTPInternalServerError(text=str(e) or type(e).__name__)
        return web.json_response(result)

    async def push_status(ws, names):
        # The poll cadence, server-side: one driver poll per subscribed
        # instrument per period, each result pushed as it lands. Api.poll
        # already folds transport failures and the self-healing reconnect
        # into the status dict, so the stream carries those too; the
        # backstop timeout matches the /api/ dispatcher's.
        try:
            while True:
                for name in names:
                    try:
                        status = await asyncio.wait_for(api.poll(name),
                                                        timeout=60)
                    except Exception as e:
                        status = {"error": str(e) or type(e).__name__,
                                  "health": False}
                    await ws.send_json({"event": "status", "instrument": name,
                                        "status": status})
                await asyncio.sleep(POLL_PERIOD)
        except ConnectionResetError:
            pass   # the peer went away mid-send; the read loop cleans up

    async def events(request):
        # The push channel: request/response stays on /api/, only unsolicited
        # server->client events ride here. The client subscribes with the
        # instrument names its panels watch; a new subscribe replaces the
        # previous one. Polling runs only while a subscribed socket is open.
        # The websocket-level heartbeat keeps a tunnelled idle connection
        # honest.
        if api is None:
            raise web.HTTPNotFound
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        pusher = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("op") == "subscribe":
                    if pusher is not None:
                        pusher.cancel()
                    pusher = asyncio.create_task(
                        push_status(ws, data.get("instruments", [])))
        finally:
            if pusher is not None:
                pusher.cancel()
        return ws

    app = web.Application()
    app.add_routes([web.get("/", shell_page),
                    web.get("/events", events),
                    web.get(r"/r/{run}/{id:\d+}/{name:.+}", resource),
                    web.post("/api/{method}", api_call)])
    return app


def build(root):
    """The application behind either front: resource registry, Api, the shared
    Surfer surface and the routed aiohttp app."""
    print("ensuring Surfer web assets ...")
    try:
        assets = ensure()
    except RuntimeError as e:
        sys.exit(str(e))
    print("using Surfer assets from", assets)
    resources = ResourceServer()
    api = Api(resources)
    api.autoconnect = root   # the shell connects to this on load, if set
    # The shared Surfer surface registers its (immutable) assets on the same
    # resource server the handler serves from; every control pane's iframe
    # points at its index URL, so the 14 MB bundle is fetched once.
    api.surfer_url = WaveformSurface(assets, resources).url
    return make_app(api, resources)


def bound_socket(host, port):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


async def run_app(app, sock):
    """Serve on an already-bound socket until cancelled. The calling loop
    becomes the rack's home: every driver socket a connect opens lives on
    it."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.SockSite(runner, sock).start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def set_app_name(name):
    """Show `name` in the macOS menu bar instead of "Python" by overriding the
    main bundle's CFBundleName before the NSApplication is built. (The Cmd-Tab
    switcher / Dock follow the Python framework's own bundle and cannot be
    overridden at runtime; that needs a standalone .app.) No-op off macOS /
    without pyobjc."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
    except Exception:
        pass


def main(root=None):
    if webview is None:
        sys.exit("pywebview is not installed (see host/gui_phase0/README.md for "
                 "the per-OS backend).")
    set_app_name("gatecap")   # menu bar; before any NSApplication is created
    # Off by default in pywebview, which then swallows a download link's
    # click; on, the click opens the platform save dialog.
    webview.settings["ALLOW_DOWNLOADS"] = True
    app = build(root)
    # The window owns the main thread (Cocoa run loop); the serving loop gets
    # a daemon thread. Binding here, before the thread starts, yields the port
    # without waiting on the loop.
    sock = bound_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    threading.Thread(target=asyncio.run, args=(run_app(app, sock),),
                     daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print("gatecap GUI on", url, flush=True)
    webview.create_window("gatecap", url=url, width=1200, height=820)
    webview.start(icon=str(ICON) if ICON.is_file() else None,
                  debug=os.environ.get("GATECAP_GUI_DEBUG") == "1")


async def serve(bind, root=None):
    """Headless front: serve the same UI to a third-party browser, on the
    caller's own loop. `bind` is ``host:port``; anything but a loopback host
    exposes an unauthenticated hardware control channel to the network, which
    is the caller's call to make (the CLI defaults to loopback, for an SSH
    tunnel to forward)."""
    host, _, port = bind.rpartition(":")
    if not host or not port.isdigit():
        sys.exit(f"--bind wants host:port, got {bind!r}")
    app = build(root)
    try:
        sock = bound_socket(host, int(port))
    except OSError as e:
        sys.exit(f"cannot bind {bind}: {e}")
    # Flushed: with stdout redirected to a file (nohup, a service manager)
    # the URL must not sit in a block buffer until something else fills it.
    print(f"gatecap GUI on http://{host}:{sock.getsockname()[1]}/", flush=True)
    try:
        await run_app(app, sock)
    except asyncio.CancelledError:
        # Ctrl-C: the runner cancelled us at the top level, and stopping is
        # exactly what was asked -- exit cleanly, no traceback.
        pass


if __name__ == "__main__":
    main()
