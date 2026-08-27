"""Headless enumeration layer over a resolved gatecap tree.

A ``Session`` resolves a transport root, enumerates the tree, and exposes it
plus the instance fingerprint. Each instrument describes itself, answers its
own status poll and supplies its own UI through its adaptor (the framework
builds the panel manifest from those); capture and VCD authoring live on the
driver nodes and their adaptors. The Session holds no UI or transport of its
own, and knows no instrument type: it reaches nodes through the protocols they
expose (``fingerprint()``, ``poll()``), never through their classes.

The methods are async (the driver is async); a GUI marshals them onto its own
event loop, a script just awaits them.
"""

from __future__ import annotations

import asyncio

from acrobe.adapter.model import get_hw_root

from .enumerator import BlockAddress, MemoryMappedInstrument


async def resolve(path):
    """Walk a resource path through the shared hw tree and return the started
    leaf node (its children are the enumerated capture blocks)."""
    hw_root = get_hw_root()
    await hw_root.ensure_started()
    leaf = await hw_root.child_summon(*path.strip("/").split("/"))
    await leaf.start_tree()
    return leaf


class Session:
    def __init__(self, root):
        self.root = root
        self.node = None
        self.fingerprint = None
        # The fd-owning transport node (see __transport_of), kept so a
        # disconnect or reconnect can tear it down and force a fresh handle.
        self.__transport = None

    async def open(self):
        """Resolve the transport, enumerate the tree, and read the instance
        fingerprint. Idempotent; returns the fingerprint."""
        self.node = await resolve(self.root)
        self.__transport = self.__transport_of(self.node)
        self.fingerprint = None
        # Every block that can report the fingerprint reports the same one --
        # the instance's -- so reading each of them costs one register read
        # apiece and leaves them all seeded: a block answering a poll from host
        # memory during a trace readback must still report it, and a missing
        # one would read as a gateware change.
        for block in self.fingerprinted():
            self.fingerprint = await block.fingerprint()
        return self.fingerprint

    @staticmethod
    def __transport_of(node):
        """The transport root for `node`: the child of the top-level adapter or
        broker (itself a direct child of the hw root). Removing this node
        stop_tree()s the whole byte-stream chain below it -- for a serial
        target that closes the tty fd -- so a later re-summon opens a fresh
        handle. The top adapter/broker is left in place: it is
        enumerator-populated (populate runs once, at hw-root start), so
        re-summoning through it needs no rescan; only the transport and what
        sits on it (framing, the capture blocks) are rebuilt."""
        hw_root = get_hw_root()
        while (node is not None and node.parent is not None
               and node.parent.parent is not hw_root):
            node = node.parent
        if (node is not None and node.parent is not None
                and node.parent.parent is hw_root):
            return node
        return None

    async def __teardown_transport(self):
        """Detach and stop the transport subtree (closing its fd), so the next
        resolve() spawns a fresh transport. Idempotent."""
        transport, self.__transport = self.__transport, None
        if transport is not None and transport.parent is not None:
            await transport.parent.child_remove(transport)

    async def reconnect(self):
        """Rebuild the transport in place: tear down the (dead) fd-owning
        subtree and re-summon a fresh one. Keeps the reference fingerprint so
        instance_changed() still fires if the target was reprogrammed while it
        was away. Raises if the device is still absent (the caller retries)."""
        await self.__teardown_transport()
        self.node = None
        self.node = await resolve(self.root)
        self.__transport = self.__transport_of(self.node)
        return self.fingerprint

    def blocks(self):
        """Every enumerated node, instruments and their blocks alike, in
        enumeration order (the console frontend asks each for its adaptor)."""
        return self.node.children_find(lambda x: True)

    def instruments(self):
        """Every enumerated instrument node, in enumeration order. The GUI
        builds its panel manifest from these alone: one instrument, one
        panel."""
        if self.node is None:
            return []
        return self.node.children_of_class(MemoryMappedInstrument)

    async def close(self):
        """Tear the transport subtree out of the shared hw tree so a later
        connect to the same root rebuilds it. child_summon caches by name, so
        removing only the leaf would leave the fd-owning transport node cached
        and its (possibly stale) handle reused -- hence the whole transport is
        dropped, closing the fd."""
        await self.__teardown_transport()
        self.node = None
        self.fingerprint = None

    # -- enumeration -------------------------------------------------------

    def blocks_of(self, kind):
        """Every enumerated block of a given driver class, in enumeration
        order -- how a caller that knows a driver type (a test, a script
        written against one instrument) finds its nodes without the Session
        knowing any of them."""
        if self.node is None:
            return []
        return self.node.children_of_class(kind)

    def fingerprinted(self):
        """The blocks that can report the instance fingerprint. Any block
        exposing ``fingerprint()`` qualifies; the descriptor CRC is the
        instance's, not the block's, so which one answers does not matter."""
        if self.node is None:
            return []
        return [block for block in self.blocks()
                if callable(getattr(block, "fingerprint", None))]

    def block_by_name(self, name):
        """Any enumerated node by address -- an instrument, a control, a
        trigger. A frontend addresses panes by name and does not know which
        kind it is talking to; a block below an instrument answers to
        "<instrument>.<block>" and, where it is unambiguous, to its bare
        name."""
        block = BlockAddress.targets(self.blocks()).get(name)
        if block is None:
            raise KeyError(f"no block {name!r}")
        return block

    def instrument_by_name(self, name):
        """One enumerated instrument by its instance name -- how a frontend
        that shows one panel per instrument addresses it."""
        for instrument in self.instruments():
            if instrument.name == name:
                return instrument
        raise KeyError(f"no instrument {name!r}")

    async def instance_changed(self):
        """True if the gateware instance changed under us (the descriptor
        fingerprint no longer matches the one read at open) -- e.g. the FPGA
        was reprogrammed or we reconnected to a different target."""
        blocks = self.fingerprinted()
        if not blocks or self.fingerprint is None:
            return False
        return await blocks[0].fingerprint() != self.fingerprint

    # -- health ------------------------------------------------------------

    async def health(self, timeout=1.0):
        """Is the target reachable? One fingerprint read under a timeout -- a
        register round trip every block answers -- so a dead or hung transport
        reports False rather than blocking the UI."""
        blocks = self.fingerprinted()
        if not blocks:
            return False
        try:
            await asyncio.wait_for(blocks[0].fingerprint(), timeout)
            return True
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return False
