"""The register files a control/status panel is built from, one subpackage
each.

Importing this package imports every block module, which registers its driver
on the enumerator's ``db`` by type UUID. A panel has exactly one: the register
file its whole map lives in.
"""

from . import registers    # noqa: F401
