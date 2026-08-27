"""The logic-analyzer instrument: gatecap's capture machinery, end to end.

Importing this package registers everything the analyzer contributes: the
``!logic-analyzer`` tag and the probe types it accepts on the generator
(:mod:`.generator`), the instrument driver and its block drivers on the
discovery layer (:mod:`.driver`, :mod:`.blocks`), and the ``capture`` command
on the ``acrobe gatecap`` group (:mod:`.cli`).

Everything capture-specific lives under here -- the VCD layout of a probe
vector (:mod:`.signals`), the VCD authoring its panes and its CLI
share (:mod:`.waveform`, :mod:`.compose`), the window planning
(:mod:`.plan`) and the readback progress (:mod:`.fetch`). The framework
around it knows none of it.
"""

from . import blocks     # noqa: F401 -- registers the block drivers
from . import cli        # noqa: F401 -- adds `acrobe gatecap capture`
from .driver import LOGIC_ANALYZER_UUID, LogicAnalyzer
from .generator import Analyzer
from .generator.signal_types import (Axi4StreamSignal, BnocFramedSignal,
                                     BnocPipeSignal, BnocSignal, BusSignal,
                                     ElementSelectedSignal, ScalarSignal)

__all__ = ["Analyzer", "Axi4StreamSignal", "BnocFramedSignal",
           "BnocPipeSignal", "BnocSignal", "BusSignal",
           "ElementSelectedSignal", "LOGIC_ANALYZER_UUID", "LogicAnalyzer",
           "ScalarSignal"]
