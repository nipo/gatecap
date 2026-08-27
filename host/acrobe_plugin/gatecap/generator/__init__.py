"""Rack generator: a YAML description in, a VHDL directory out.

A description names a rack, its transport and the instruments it holds.
Emission is a package, a backplane, a rack entity, and whatever the
instruments write next to them (:class:`RackAssembly`); the address map is
allocated at elaboration out of the descriptor's own instrument envelopes, so
the generator never knows it and never has to.

Three plugin registries carry everything type-specific:
:class:`InstrumentRegistry` keyed by the YAML tag of an ``instruments`` entry,
:class:`CommunicationRegistry` keyed by the transport mode, and
:class:`SignalTypeRegistry` keyed by the YAML tag of a probed signal, which
the logic analyzer consumes.

Nothing type-specific lives here: the transports gatecap ships register from
:mod:`acrobe_plugin.gatecap.communication`, its logic analyzer and the probe
types that serve it from :mod:`acrobe_plugin.gatecap.instrument.la`, and a
third-party plugin from its own package. Importing anything under
``acrobe_plugin.gatecap`` runs those registrations, so the registries are
fully formed by the time a description is parsed.

The VHDL code model plugins emit through (:mod:`.vhdl`) is re-exported here,
so a plugin takes its base classes, its registries and the nodes it builds
declarations and statements from out of one namespace.
"""

from .checks import Check
from .cdc import Cdc, ClockDomain
from .communication import (CommunicationContext, CommunicationPlugin,
                            CommunicationRegistry, HostClock)
from .enums import EnumSpec
from .errors import DescriptionError
from .fields import Field
from .gbs import GbsManifest
from .instruments import (InstrumentContext, InstrumentContribution,
                          InstrumentPlugin, InstrumentRegistry)
from .loader import Tagged, YamlSource
from .parser import DescriptionParser
from .rack import RackAssembly
from .schema import Communication, Description, Instrument, Name
from .signal_types import SignalTypePlugin, SignalTypeRegistry
from .vhdl import (Architecture, Assignment, Comment, ComponentDecl, Constant,
                   DesignFile, Entity, Expr, FunctionBody, FunctionDecl,
                   Generic, Identifier, Instance, Package, PackageBody, Port,
                   Process, RawStatement, SignalDecl)


class Generator:
    """What a description becomes: one rack."""

    @staticmethod
    def of(description):
        return RackAssembly(description)


__all__ = [
    "Architecture", "Assignment",
    "Cdc", "Check", "ClockDomain", "Comment", "Communication",
    "CommunicationContext", "CommunicationPlugin", "CommunicationRegistry",
    "ComponentDecl", "Constant",
    "Description", "DescriptionError", "DescriptionParser", "DesignFile",
    "Entity", "EnumSpec", "Expr", "Field",
    "FunctionBody", "FunctionDecl",
    "GbsManifest", "Generator", "Generic",
    "HostClock", "Identifier", "Instance", "Instrument", "InstrumentContext",
    "InstrumentContribution",
    "InstrumentPlugin", "InstrumentRegistry",
    "Name",
    "Package", "PackageBody", "Port", "Process",
    "RackAssembly", "RawStatement",
    "SignalDecl", "SignalTypePlugin", "SignalTypeRegistry",
    "Tagged",
    "YamlSource",
    ]
