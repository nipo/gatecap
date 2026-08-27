"""GUI framework tests: the id()-addressed resource route (a plain owner and
the shared Surfer surface), the /api/ HTTP route the shell calls through, and
the Api -> driver flow (connect, poll, and the generic instrument_message
router), all on one asyncio loop as the server runs it. The pywebview window
is not exercised.

Run: python3.13 -m pytest host/tests/test_gui.py
"""

import asyncio
import os
import subprocess
import time

import pytest

from aiohttp.test_utils import TestClient, TestServer

from acrobe.adapter.model import reset_hw_root_for_tests
from acrobe_plugin.gatecap.gui.app import Api, make_app
from acrobe_plugin.gatecap.gui.resources import ResourceServer, WaveformSurface
from acrobe_plugin.gatecap.instrument.la.blocks.control import Control

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM_DIR = os.path.join(REPO, "gateware", "example", "socket")
RESOURCE = "udp/127.0.0.1:4242/gatecap"


def _client(resources, api=None):
    return TestClient(TestServer(make_app(api, resources)))


class _Owner:
    """A minimal resource owner: serves one named blob."""

    def resource(self, name):
        if name == "trace.vcd":
            return b"$enddefinitions $end\n", "text/plain"
        return None


def test_resource_route():
    # Everything but "/" and /api/ is /r/<run>/<owner-id>/<name>, resolved
    # through the registry to the owning object's bytes.
    resources = ResourceServer()
    url = resources.mint(_Owner(), "trace.vcd")  # no-store (dynamic)

    async def run():
        async with _client(resources) as c:
            r = await c.get("/")
            assert r.status == 200 and r.content_type == "text/html"
            assert b"<html" in (await r.read()).lower()

            r = await c.get(url)
            assert r.status == 200 and r.content_type == "text/plain"
            assert await r.read() == b"$enddefinitions $end\n"
            assert r.headers["Cache-Control"] == "no-store"

            parent = url.rsplit("/", 1)[0]
            for bad in ["/nope.js", parent + "/nope",   # unknown resource name
                        "/r/deadbeef/1/trace.vcd"]:     # wrong run / unknown id
                assert (await c.get(bad)).status == 404

    asyncio.run(run())


def test_surfer_surface(tmp_path):
    # The shared surface content-addresses Surfer's assets and rewrites the
    # index to point at the registry; the heavy assets are served immutable.
    (tmp_path / "index.html").write_text(
        "<script type=\"module\">import init from '/surfer.js';"
        " await init({module_or_path: '/surfer_bg.wasm'});</script>"
        '<link rel="manifest" href="manifest.json">'
        '<link rel="modulepreload" href="/surfer.js" integrity="sha384-x">')
    (tmp_path / "surfer.js").write_text("// js")
    (tmp_path / "surfer_bg.wasm").write_bytes(b"\0asm")
    (tmp_path / "manifest.json").write_text("{}")

    resources = ResourceServer()
    surface = WaveformSurface(tmp_path, resources)
    idx = surface.resource("index.html")[0].decode()
    assert "'/surfer.js'" not in idx and 'href="/surfer.js"' not in idx
    assert '"manifest.json"' not in idx
    assert "/r/" in idx and "integrity" not in idx
    assert surface.url.endswith("#dev")

    async def run():
        async with _client(resources) as c:
            r = await c.get(surface.url.split("#")[0])
            assert r.status == 200 and r.content_type == "text/html"
            assert b"/r/" in await r.read()
            # a Surfer bump changes the id -> busts
            assert "immutable" in r.headers["Cache-Control"]

    asyncio.run(run())


def test_api_route():
    # The shell's one transport: POST /api/<method> with the JSON argument
    # list dispatches to the Api; unknown methods and non-exposed attributes
    # 404, a raising call 500s.
    resources = ResourceServer()
    api = Api(resources)
    api.autoconnect = "udp/10.0.0.1:4242/gatecap"

    async def run():
        async with _client(resources, api) as c:
            r = await c.post("/api/autoconnect_root", json=[])
            assert r.status == 200
            assert await r.json() == "udp/10.0.0.1:4242/gatecap"

            r = await c.post("/api/poll", json=["la"])  # not connected
            assert r.status == 200 and (await r.json())["health"] is False

            for bad in ["/api/resolve_instrument",   # public but not exposed
                        "/api/nonsense"]:
                assert (await c.post(bad, json=[])).status == 404

            r = await c.post("/api/poll", json=[])   # arity mismatch raises
            assert r.status == 500

    asyncio.run(run())


def test_events_route():
    # The push channel: a subscribe starts the server-side poll cadence and
    # statuses stream back unsolicited. Not connected here, so every status
    # is the Api's own not-connected error -- the stream itself carries
    # failures, it never goes silent.
    resources = ResourceServer()
    api = Api(resources)

    async def run():
        async with _client(resources, api) as c:
            ws = await c.ws_connect("/events")
            await ws.send_json({"op": "subscribe", "instruments": ["la"]})
            for _ in range(2):   # cadence: more than one arrives unprompted
                m = await asyncio.wait_for(ws.receive_json(), 10)
                assert m["event"] == "status" and m["instrument"] == "la"
                assert m["status"]["health"] is False
            await ws.close()

    asyncio.run(run())


@pytest.fixture(scope="module")
def sim():
    subprocess.run(["pkill", "-9", "-f", "gateware/example/socket"], capture_output=True)
    time.sleep(0.5)
    build = subprocess.run(["gbs", "project", "build"], cwd=SIM_DIR,
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    sim_bin = os.path.join(SIM_DIR, "tb")
    assert os.path.exists(sim_bin)
    proc = subprocess.Popen([sim_bin, "--ieee-asserts=disable"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    reset_hw_root_for_tests()  # fresh hw tree, not one cached by another module
    try:
        yield
    finally:
        reset_hw_root_for_tests()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["pkill", "-9", "-f", "gateware/example/socket"], capture_output=True)


def _vcd_of(api, name):
    # The trace bytes the panel authored, as the resource route would serve.
    return api.resolve_instrument(name).resource("trace.vcd")[0]


def test_api_capture(sim):
    # The Api routes panel ops through instrument_message and hands the shell
    # the server-minted panel/trace URLs -- the pattern every panel depends
    # on. Driver awaits run right on the test's loop, as on the server's.
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        api = Api(ResourceServer())

        res = await api.connect(RESOURCE)
        assert "error" not in res, res
        # Self-describing manifest: one entry per instrument, each supplying
        # its own panel. The blocks it is built from get no entry of their own
        # -- the trigger and the capture domain are sections of the analyzer's
        # panel.
        instruments = res["describe"]["instruments"]
        (analyzer,) = instruments
        assert analyzer["name"] == "la" and analyzer["grouped"] is False
        assert [m["name"] for m in analyzer["members"]] == ["control.control"]
        (trigger,) = analyzer["triggers"]
        assert trigger["name"] == "control.trigger" and trigger["kind"] == "value"
        assert trigger["signal_count"] == 8 and len(trigger["fields"]) == 8
        assert analyzer["panel_url"].startswith("/r/")  # server-minted URL

        st = await api.poll("la")
        # The instrument names and tones its own state; the framework knows
        # neither, and no state encoding reaches the shell.
        assert st["health"] is True and st["state"] in ("idle", "armed", "capturing")
        assert st["tone"] in ("idle", "active", "attention")
        assert st["changed"] is False  # same instance
        assert st["fingerprint"] == res["describe"]["fingerprint"]
        assert isinstance(st["latency_ms"], (int, float)) and st["latency_ms"] >= 0

        # The log pane's target dump is the `gatecap info` text, which still
        # describes every block.
        info = api.info()
        assert any("probes" in l for l in info) and any("sample clock" in l for l in info)

        cap = await api.instrument_message(
            "la", {"op": "capture",
                   "params": {"value": 0x80, "mask": 0xFF,
                              "overrides": {"control.control": {"count": 4}}}})
        assert "error" not in cap, cap
        assert cap["serial"] >= 1 and cap["scopes"] == ["capture"]
        assert cap["trace_url"].startswith("/r/") and "?t=" in cap["trace_url"]
        vcd = _vcd_of(api, "la")
        assert vcd.startswith(b"$") and b"$enddefinitions" in vcd

        # Disconnect tears the subtree down; reconnecting to the same root
        # re-enumerates (no stale-cache bridge error) and still works.
        assert "error" not in await api.disconnect()
        res2 = await api.connect(RESOURCE)
        assert "error" not in res2, res2
        assert [i["name"] for i in res2["describe"]["instruments"]] == ["la"]
        assert (await api.poll("la"))["health"] is True

    asyncio.run(run())


def test_events_stream(sim):
    # Against live hardware the stream carries the same statuses the /api/poll
    # route answers with: state, tone, health, fingerprint, RTT. A second
    # subscribe replaces the first (the shell resubscribes after re-rendering
    # its panels).
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        resources = ResourceServer()
        api = Api(resources)

        async with _client(resources, api) as c:
            r = await c.post("/api/connect", json=[RESOURCE])
            res = await r.json()
            assert "error" not in res, res

            ws = await c.ws_connect("/events")
            await ws.send_json({"op": "subscribe", "instruments": ["la"]})
            for _ in range(2):
                m = await asyncio.wait_for(ws.receive_json(), 10)
                assert m["instrument"] == "la"
                st = m["status"]
                assert st["health"] is True
                assert st["state"] in ("idle", "armed", "capturing")
                assert st["fingerprint"] == res["describe"]["fingerprint"]
                assert isinstance(st["latency_ms"], (int, float))

            await ws.send_json({"op": "subscribe", "instruments": ["nope"]})
            # The old pusher may have one "la" in flight; the replacement then
            # streams the unknown name's error status.
            for _ in range(3):
                m = await asyncio.wait_for(ws.receive_json(), 10)
                if m["instrument"] == "nope":
                    break
            assert m["instrument"] == "nope" and m["status"]["health"] is False

            await ws.close()
            await c.post("/api/disconnect", json=[])

    asyncio.run(run())


def test_api_arm_read(sim):
    # The interactive flow: arm (returns immediately), poll to completion,
    # then read -- as the GUI does for DUT-driven captures. Every op reaches
    # the analyzer, which fans out to the blocks its panel drives.
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        api = Api(ResourceServer())
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        # Decoupled: the trigger editor writes the compare, Arm just arms.
        assert "error" not in await api.instrument_message(
            "la", {"op": "configure",
                   "triggers": {"control.trigger": {"value": 0, "mask": 0}}})  # match-all
        params = {"overrides": {"control.control": {"count": 8, "pretrigger": 2}}}
        assert "error" not in await api.instrument_message(
            "la", {"op": "arm", "params": params})

        st = None
        for _ in range(50):  # match-all trigger returns to idle quickly
            st = await api.poll("la")
            if st["state"] == "idle":
                break
            await asyncio.sleep(0.05)
        assert st["state"] == "idle" and st["triggered"]

        rd = await api.instrument_message("la", {"op": "read", "params": params})
        assert "error" not in rd and rd["scopes"] == ["capture"]
        assert rd["trace_url"].startswith("/r/")
        vcd = _vcd_of(api, "la")
        assert vcd.startswith(b"$") and b"$enddefinitions" in vcd

    asyncio.run(run())


def test_api_reconnect_replays_trigger(sim):
    # A device reset wipes the trigger registers AND drops the transport. The
    # self-healing poll must rebuild the transport *and* replay the cached
    # trigger compare -- otherwise a re-armed capture fires instantly on the
    # match-any reset default (mask 0) instead of the configured value.
    async def run():
        reset_hw_root_for_tests()  # own hw tree, bound to this test's loop
        api = Api(ResourceServer())
        res = await api.connect(RESOURCE)
        assert "error" not in res, res

        # Configure a value trigger through the Api: writes hw and is cached.
        assert "error" not in await api.instrument_message(
            "la", {"op": "configure",
                   "triggers": {"control.trigger": {"value": 0x80, "mask": 0xFF}}})

        async def arm_first_sample():
            # The interactive path: arm (writes only length/pre-trigger, never
            # the trigger), poll to idle, then read the first captured sample.
            # The free-running counter makes the value trigger land
            # deterministically.
            params = {"overrides": {"control.control": {"count": 4, "pretrigger": 0}}}
            assert "error" not in await api.instrument_message(
                "la", {"op": "arm", "params": params})
            for _ in range(200):
                if (await api.poll("la")).get("state") == "idle":
                    break
                await asyncio.sleep(0.02)
            r = await api.session.blocks_of(Control)[0].read_trace(
                count=4, pretrigger=0)
            return [x for w in r["windows"] for x in w][0]

        assert await arm_first_sample() == 0x80   # triggers on the configured value

        # Model the reset: wipe the trigger registers (match-any) and drop the
        # transport, so the next poll hits a dead handle and must self-heal.
        await api.session.blocks_of(Control)[0].trigger_node_get().configure(0, 0)
        await api.session._Session__teardown_transport()
        st = await api.poll("la")    # -> reconnect + replay of the compare
        assert st.get("reconnected") is True   # the shell logs this + re-dumps info
        assert any(r["instrument"] == "la" and "value=0x80" in r["summary"]
                   for r in st.get("replayed", []))

        assert await arm_first_sample() == 0x80   # replay restored the trigger

    asyncio.run(run())
