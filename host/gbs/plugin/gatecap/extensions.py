"""Instruments, signal types and modes that other acrobe plugins add.

gatecap's generator only knows what has been registered with it, and an
extension registers by being imported, which acrobe's plugin loading does
for a command line but nothing does for gbs.  A description naming an
extension's tag is parsed only after this has run.
"""


class Extensions:
    loaded = False

    @classmethod
    def load(cls):
        """Import every acrobe plugin, once per process."""
        if cls.loaded:
            return
        from acrobe.plugin import load_plugins

        load_plugins()
        cls.loaded = True
