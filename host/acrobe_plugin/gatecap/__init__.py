"""gatecap acrobe plugin.

Importing this package runs the registrations that wire gatecap into acrobe:
the "gatecap" handler on ``Datagram.db`` and on ``spi.Target.child_db``, the
USB adapter a rack on the bus is recognised by, the ``acrobe gatecap`` CLI
group, the transports a rack can be reached over, and the instruments gatecap
ships. The plugin loader only imports this top-level package, so every
submodule with registrations is imported here.

An in-tree instrument registers exactly the way a third-party one does -- by
being imported, and calling the registries from its own package -- so the
framework below it holds no knowledge of any instrument type.
"""

from . import bridge         # noqa: F401 -- registers Datagram.db "gatecap"
from . import cli            # noqa: F401 -- adds the `acrobe gatecap` CLI group
from . import communication  # noqa: F401 -- registers the transport modes
from . import spi          # noqa: F401 -- registers the SPI rack node
from . import usb          # noqa: F401 -- registers the USB rack adapter
from .instrument import bus_explorer  # noqa: F401 -- registers the explorer
from .instrument import clock_measurer  # noqa: F401 -- registers the measurer
from .instrument import control_status  # noqa: F401 -- registers the panel
from .instrument import la   # noqa: F401 -- registers the logic analyzer
