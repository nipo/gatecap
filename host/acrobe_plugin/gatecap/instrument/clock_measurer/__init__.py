"""The clock-measurer instrument: the rate of several clocks, live.

One reference clock carries the time base -- the only clock whose rate the
design states -- and every other clock the instrument watches is counted
against it and published as a rate in Hz. There is nothing to arm and nothing
to configure: the measurement is free-running from reset, so the instrument is
one register file and the rate array is its whole surface.

Importing this package registers everything the instrument contributes: the
``!clock-measurer`` tag on the generator (:mod:`.generator`), which emits a
``gatecap.clock_measurer.clock_rate_block`` and its envelope out of a
description naming the reference and the observed clocks; the instrument
driver with its pane on the discovery layer (:mod:`.driver`); and the
``rates`` command on the ``acrobe gatecap`` group (:mod:`.cli`).

It was developed out of tree, as an extension package of its own, before
moving in: it registers exactly the way a third-party instrument does -- by
being imported, and calling the registries from its own package.
"""

from . import cli          # noqa: F401 -- adds `acrobe gatecap rates`
from . import generator    # noqa: F401 -- registers the !clock-measurer tag
from .driver import CLOCK_MEASURER_UUID, ClockMeasurer, Rate

__all__ = ["CLOCK_MEASURER_UUID", "ClockMeasurer", "Rate"]
