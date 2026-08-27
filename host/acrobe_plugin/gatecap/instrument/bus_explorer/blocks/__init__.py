"""The register files a bus explorer is built from, one subpackage each.

Importing this package imports every block module, which registers its driver
on the enumerator's ``db`` by type UUID. An explorer has exactly one: the
engine, whose register file is the whole instrument.
"""

from . import engine    # noqa: F401
