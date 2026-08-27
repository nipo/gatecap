"""The register files a logic analyzer is built from, one subpackage each.

Importing this package imports every block module, which registers its
driver(s) on the enumerator's ``db`` by type UUID. A third-party block is
added the same way, from the plugin that owns it.
"""

from . import buffer     # noqa: F401
from . import trigger    # noqa: F401
from . import control    # noqa: F401
