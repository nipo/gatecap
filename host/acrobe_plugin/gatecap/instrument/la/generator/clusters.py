"""Per-domain elaboration: probe vectors, geometry and the block cluster.

One capturing domain becomes a control block, a capture core, a trace buffer
and the crossings between the register clock and the capture clock. The two
storage styles -- raw multi-window and run-length encoded -- are distinct block
types with distinct configuration and readback, so they are distinct clusters
here too.

Everything that depends on a stream configuration generic (vector widths,
sample stride, buffer size) is emitted as an elaboration-time expression, not
computed here: the generated core resolves it when the instantiating design
supplies the configuration.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (Assignment, Cdc, Check,
                                             Constant, Expr, Instance,
                                             Process, SignalDecl)


class ProbeVector:
    """The capture or trigger vector of one domain.

    Probes take the vector in description order, first probe at the low bits;
    the name-spec lists them in the same order, so a name always sits over the
    bits it labels."""

    TRACE = "trace"
    TRIGGER = "trigger"

    def __init__(self, probes, kind):
        assert kind in (self.TRACE, self.TRIGGER), f"no {kind} vector"
        self.kind = kind
        self.probes = tuple(probes)

    def empty(self):
        return not self.probes

    def count(self):
        """Bit count as an elaboration-time expression."""
        if self.empty():
            return "0"
        return Expr.wrapped_join(
            [self.__of(probe, "length") for probe in self.probes], " + ")

    def pack(self):
        """Concatenation of the packed probes, low bits first."""
        assert not self.empty(), "an empty vector has nothing to pack"
        return Expr.wrapped_join(
            [self.__of(probe, "pack") for probe in reversed(self.probes)])

    def names(self):
        """The comma-separated name-spec, in bit order."""
        if self.empty():
            return Expr.string("")
        parts = []
        for probe in self.probes:
            if parts:
                parts.append(Expr.string(","))
            parts.append(self.__of(probe, "names"))
        return Expr.wrapped_join(parts)

    def static_width(self):
        """Bit count when every probe's width is fixed by the description,
        else None: a stream contributes a width only the gateware knows."""
        total = 0
        for probe in self.probes:
            width = self.__of(probe, "width")
            if width is None:
                return None
            total += width
        return total

    def __of(self, probe, what):
        return getattr(probe.plugin, f"{self.kind}_{what}")(probe)


class TraceGeometry:
    """The trace buffer's footprint, as ``gatecap.trace`` computes it.

    The generated core derives it at elaboration, from constants that may
    depend on generics. Where the description fixes every probe width the same
    numbers are reachable here, which is what lets the address map be laid out
    before elaboration; the map's own elaboration-time assertion keeps the two
    derivations from drifting apart."""

    @staticmethod
    def round_up_l2(value):
        """log2 of the smallest power of two that is at least ``value``."""
        l2 = 0
        while (1 << l2) < value:
            l2 += 1
        return l2

    @classmethod
    def packed_lane_l2(cls, sample_width, word_bytes):
        """Address LSBs the byte-lane index of a packed sample takes."""
        lane_bytes = 1 << cls.round_up_l2((sample_width + 7) // 8)
        return cls.round_up_l2(word_bytes // lane_bytes)

    @classmethod
    def line_word_l2(cls, sample_width, word_bits):
        """Address LSBs the in-line word index of a wide sample takes."""
        return cls.round_up_l2((sample_width + word_bits - 1) // word_bits)

    @classmethod
    def buffer_size_l2(cls, buffer_depth_l2, signal_count, data_bus_width_l2,
                       packed, rle):
        """Bytes of address space a trace buffer occupies, log2."""
        word_bytes = 1 << data_bus_width_l2
        word_bits = 8 * word_bytes
        # The RLE tag rides one extra bit per line.
        sample_width = signal_count + (1 if rle else 0)
        words_l2 = buffer_depth_l2
        # Packed (several samples per word) and wide (a run of words per
        # sample) are mutually exclusive: at most one term is non-zero.
        if packed:
            words_l2 -= cls.packed_lane_l2(signal_count, word_bytes)
        words_l2 += cls.line_word_l2(sample_width, word_bits)
        return words_l2 + data_bus_width_l2


class DomainCluster:
    """The block cluster of one capturing domain.

    Signal and constant names are prefixed with the domain name, so several
    clusters coexist in one architecture without a naming scheme of their
    own."""

    DEPS = ("gatecap.capture", "gatecap.control", "gatecap.trace")
    APB_WORD_BITS = "word_bits_c"
    APB_WORD_BYTES = "word_bytes_c"
    TRIGGER_WIDTH_MAX = 32

    def __init__(self, domain, host, capture, trigger, regions):
        self.domain = domain
        self.host = host
        self.capture = capture
        self.trigger = trigger
        self.regions = regions
        self.down = Cdc(host, capture)
        self.up = Cdc(capture, host)
        self.vector = ProbeVector(domain.traced_probes(), ProbeVector.TRACE)
        self.blocks = [self.control(), self.core(), self.buffer()]

    @classmethod
    def of(cls, domain, host, capture, trigger, regions):
        """The cluster implementing this domain's storage."""
        style = RleCluster if domain.storage.rle else RawCluster
        return style(domain, host, capture, trigger, regions)

    # Names

    def name(self, suffix):
        return f"{self.domain.name}_{suffix}"

    def constant(self, suffix):
        return self.name(f"{suffix}_c")

    def signal(self, suffix):
        return self.name(f"{suffix}_s")

    def deps(self):
        return self.DEPS + self.down.deps() + self.up.deps()

    # Geometry

    def signal_count(self):
        return self.constant("signal_count")

    def line_width(self):
        return self.constant("line_width")

    def depth_l2(self):
        return self.constant("buffer_depth_l2")

    def buffer_size_l2(self):
        return self.constant("buffer_size_l2")

    def static_buffer_size_l2(self, data_bus_width_l2):
        """The same footprint as a number, or None when a probe's width is
        only known once the core elaborates."""
        signal_count = self.vector.static_width()
        if signal_count is None:
            return None
        return TraceGeometry.buffer_size_l2(
            self.domain.storage.buffer_depth_l2, signal_count,
            data_bus_width_l2, self.domain.storage.packed,
            self.domain.storage.rle)

    def sample_stride(self):
        return self.constant("sample_stride")

    def trigger_latency(self):
        return self.constant("trigger_latency")

    def tag_bits(self):
        """Bits the storage adds to a sample on its way to the buffer."""
        return 0

    def geometry(self):
        packed = self.domain.storage.packed
        if packed:
            stride = Expr.call("gatecap.trace.packed_lane_bytes", self.signal_count(),
                               self.APB_WORD_BYTES) + " * 8"
        else:
            stride = Expr.call("gatecap.trace.line_word_count", self.line_width(),
                               self.APB_WORD_BITS) + f" * {self.APB_WORD_BITS}"
        tag = self.tag_bits()
        return [
            Constant(self.signal_count(), "natural", self.vector.count(),
                     comment=f"Capture vector of domain {self.domain.name}: "
                             "one field per probe, first probe at the low "
                             "bits."),
            Constant(self.constant("signal_names"), "string",
                     self.vector.names()),
            Constant(self.depth_l2(), "natural",
                     str(self.domain.storage.buffer_depth_l2)),
            Constant(self.line_width(), "natural",
                     self.signal_count() if not tag
                     else f"{self.signal_count()} + {tag}",
                     comment=None if not tag else
                     "One tag bit rides alongside each stored line."),
            Constant(self.sample_stride(), "natural", stride,
                     comment="Read-side stride the host walks the buffer "
                             "with: a byte lane for a packed sample, the run "
                             "of words a line occupies otherwise."),
            Constant(self.buffer_size_l2(), "natural",
                     Expr.wrapped_call(
                         "gatecap.trace.buffer_size_l2", self.depth_l2(),
                         self.signal_count(), "data_bus_width_l2_c",
                         Expr.boolean(packed),
                         Expr.boolean(self.domain.storage.rle)),
                     comment="Bytes of address space the trace buffer "
                             "occupies, log2."),
            Constant(self.trigger_latency(), "natural",
                     f"{self.trigger.latency_constant()} + "
                     f"{self.trigger.integration_constant()}",
                     comment="Cycles from the matched sample to the core's "
                             "trigger input: the trigger block's own latency "
                             "plus the wiring in between. The core back-dates "
                             "the trigger sample by this much."),
            ] + self.storage_geometry()

    def storage_geometry(self):
        """Constants only one storage style has."""
        return []

    def checks(self):
        """Checks on geometry the description alone cannot decide."""
        if not self.domain.storage.packed:
            return []
        return [Check(
            self.name("packing_check"),
            f"domain {self.domain.name}: a byte-lane packed sample must fit "
            "one APB word",
            f"{self.line_width()} <= {self.APB_WORD_BITS}")]

    # Structure

    def declarations(self):
        return (self.geometry() + self.signals()
                + self.down.declarations + self.up.declarations)

    def statements(self):
        return ([Assignment(
            self.signal("signals"), self.vector.pack(),
            comment=f"Domain {self.domain.name}: the capture vector, then the "
                    "blocks that store it. Probes enter the vector in "
                    "description order, the first one at the low bits.")]
                + self.down.statements + self.up.statements + self.blocks)

    def signals(self):
        return [
            SignalDecl(self.signal("signals"),
                       f"std_ulogic_vector({self.signal_count()}-1 downto 0)"),
            SignalDecl(self.signal("arm"), "std_ulogic"),
            SignalDecl(self.signal("abort"), "std_ulogic"),
            SignalDecl(self.signal("state"), "std_ulogic_vector(1 downto 0)"),
            SignalDecl(self.signal("triggered"), "std_ulogic"),
            SignalDecl(self.signal("ready"), "std_ulogic",
                       comment="Armed with the pre-trigger window filled: "
                               "what enables the trigger block."),
            SignalDecl(self.signal("write_en"), "std_ulogic"),
            SignalDecl(self.signal("write_addr"),
                       f"unsigned({self.depth_l2()}-1 downto 0)"),
            SignalDecl(self.signal("write_data"),
                       f"std_ulogic_vector({self.line_width()}-1 downto 0)"),
            ] + self.storage_signals()

    def status(self):
        """Status crossed back into the register domain, as the control
        block's port associations."""
        return {
            "state_i": self.up.level(self.signal("state_host"),
                                     self.signal("state"), "2",
                                     stable=Cdc.STATE_STABLE_COUNT),
            "triggered_i": self.up.flag(self.signal("triggered_host"),
                                        self.signal("triggered")),
            "ready_i": self.up.flag(self.signal("ready_host"),
                                    self.signal("ready")),
            }

    def commands(self):
        """Arm and abort crossed into the capture domain."""
        return {
            "arm_i": self.down.tick(self.signal("arm_cap"),
                                    self.signal("arm")),
            "abort_i": self.down.tick(self.signal("abort_cap"),
                                      self.signal("abort")),
            }

    def buffer(self):
        packed = self.domain.storage.packed
        unit = ("gatecap.trace.trace_buffer_packed" if packed
                else "gatecap.trace.trace_buffer")
        return Instance(
            self.name("buffer"), unit,
            generic_map={
                "apb_config_c": "apb_config_c",
                "sample_width_c": self.line_width(),
                "depth_l2_c": self.depth_l2(),
                },
            port_map={
                "clock_i": self.host.clock,
                "reset_n_i": self.host.reset_n,
                "apb_i": self.regions.buffer.master(),
                "apb_o": self.regions.buffer.slave(),
                "write_clock_i": self.capture.clock,
                "write_en_i": self.signal("write_en"),
                "write_addr_i": self.signal("write_addr"),
                "write_data_i": self.signal("write_data"),
                })

    def storage_signals(self):
        raise NotImplementedError

    def control(self):
        raise NotImplementedError

    def core(self):
        raise NotImplementedError

    # Descriptor

    def buffer_object(self):
        return Expr.wrapped_call("buffer_desc", self.sample_stride(),
                                 self.buffer_size_l2())

    def control_object(self):
        raise NotImplementedError

    def control_arguments(self, **storage):
        arguments = {
            "buffer_name": Expr.string(self.regions.buffer.key),
            "trigger_name": Expr.string(self.trigger.key),
            "signal_count": self.signal_count(),
            "signal_names": self.constant("signal_names"),
            }
        arguments.update(storage)
        arguments.update({
            "capture_clock_hz": str(self.domain.frequency),
            "integration_latency": self.trigger.integration_constant(),
            })
        return arguments


class RawCluster(DomainCluster):
    """Raw storage: one sample per line, one or several windows per run, the
    host reading back a head pointer per window."""

    CONTROL = "gatecap.control.capture_control"
    CORE = "gatecap.capture.capture_core"

    def len_width(self):
        return self.constant("capture_len_width")

    def storage_geometry(self):
        # A length field one bit wider than the buffer depth, so a whole-buffer
        # capture -- and the window count, which shares the field width -- are
        # expressible.
        return [Constant(self.len_width(), "natural",
                         str(self.domain.storage.buffer_depth_l2 + 1))]

    def storage_signals(self):
        return [
            SignalDecl(self.signal("capture_len"),
                       f"unsigned({self.len_width()}-1 downto 0)"),
            SignalDecl(self.signal("pre_trigger_len"),
                       f"unsigned({self.len_width()}-1 downto 0)"),
            SignalDecl(self.signal("window_count"),
                       f"unsigned({self.len_width()}-1 downto 0)"),
            SignalDecl(self.signal("head"),
                       f"unsigned({self.depth_l2()}-1 downto 0)"),
            SignalDecl(self.signal("head_we"), "std_ulogic"),
            ]

    def head(self):
        """The per-window head as the control block sees it.

        In the capture domain the core's head is valid for one cycle; crossing
        it means holding it there and pairing the resynchronised value with a
        completion tick, so the control latches a settled value."""
        if self.up.direct():
            return {"head_i": self.signal("head"),
                    "head_we_i": self.signal("head_we")}
        self.up.declarations.append(
            SignalDecl(self.signal("head_hold"),
                       f"unsigned({self.depth_l2()}-1 downto 0)"))
        self.up.statements.append(Process(
            self.name("head_hold"), (self.capture.clock,),
            f"if rising_edge({self.capture.clock}) then\n"
            f"  if {self.signal('head_we')} = '1' then\n"
            f"    {self.signal('head_hold')} <= {self.signal('head')};\n"
            "  end if;\n"
            "end if;"))
        return {
            "head_i": self.up.level(self.signal("head_host"),
                                    self.signal("head_hold"), self.depth_l2(),
                                    numeric=True),
            "head_we_i": self.up.tick(self.signal("head_we_host"),
                                      self.signal("head_we")),
            }

    def control(self):
        port_map = {
            "clock_i": self.host.clock,
            "reset_n_i": self.host.reset_n,
            "apb_i": self.regions.control.master(),
            "apb_o": self.regions.control.slave(),
            "arm_o": self.signal("arm"),
            "abort_o": self.signal("abort"),
            "capture_len_o": self.signal("capture_len"),
            "pre_trigger_len_o": self.signal("pre_trigger_len"),
            "window_count_o": self.signal("window_count"),
            "enable_o": "open",
            }
        port_map.update(self.status())
        port_map.update(self.head())
        return Instance(
            self.name("control"), self.CONTROL,
            generic_map={
                "apb_config_c": "apb_config_c",
                "capture_len_width_c": self.len_width(),
                "depth_l2_c": self.depth_l2(),
                "window_count_c": str(self.domain.capture.max_windows),
                "fingerprint_c": "fingerprint_c",
                },
            port_map=port_map)

    def core(self):
        port_map = {
            "clock_i": self.capture.clock,
            "reset_n_i": self.capture.reset_n,
            "signals_i": self.signal("signals"),
            }
        port_map.update(self.commands())
        port_map.update({
            "trigger_i": self.trigger.tick(),
            "capture_len_i": self.down.static(
                self.signal("capture_len_cap"), self.signal("capture_len"),
                self.len_width(), numeric=True),
            "pre_trigger_len_i": self.down.static(
                self.signal("pre_trigger_len_cap"),
                self.signal("pre_trigger_len"), self.len_width(),
                numeric=True),
            "window_count_i": self.down.static(
                self.signal("window_count_cap"), self.signal("window_count"),
                self.len_width(), numeric=True),
            "state_o": self.signal("state"),
            "triggered_o": self.signal("triggered"),
            "ready_o": self.signal("ready"),
            "head_o": self.signal("head"),
            "head_we_o": self.signal("head_we"),
            "write_en_o": self.signal("write_en"),
            "write_addr_o": self.signal("write_addr"),
            "write_data_o": self.signal("write_data"),
            })
        return Instance(
            self.name("core"), self.CORE,
            generic_map={
                "signal_count_c": self.signal_count(),
                "capture_len_width_c": self.len_width(),
                "depth_l2_c": self.depth_l2(),
                "window_count_c": str(self.domain.capture.max_windows),
                "trigger_latency_c": self.trigger_latency(),
                },
            port_map=port_map)

    def control_object(self):
        return Expr.wrapped_call("control_desc", **self.control_arguments(
            capture_len_width=self.len_width(),
            window_count=str(self.domain.capture.max_windows)))


class RleCluster(DomainCluster):
    """Run-length-encoded storage: one line per signal change, the host
    reading back the ring pointers and decoding from the start."""

    CONTROL = "gatecap.control.capture_control_rle"
    CORE = "gatecap.capture.capture_core_rle"
    COUNT_BITS = 32

    def tag_bits(self):
        return 1

    def pointer_width(self):
        return self.constant("pointer_width")

    def storage_geometry(self):
        # Ring pointers span the buffer plus the wrap bit.
        return [Constant(self.pointer_width(), "natural",
                         f"{self.depth_l2()} + 1")]

    def storage_signals(self):
        return [
            SignalDecl(self.signal("pre_lines"),
                       f"unsigned({self.pointer_width()}-1 downto 0)"),
            SignalDecl(self.signal("max_cycles"), "unsigned(31 downto 0)"),
            SignalDecl(self.signal("end_ptr"),
                       f"unsigned({self.pointer_width()}-1 downto 0)"),
            SignalDecl(self.signal("pre_head"),
                       f"unsigned({self.depth_l2()}-1 downto 0)"),
            SignalDecl(self.signal("pre_n"),
                       f"unsigned({self.pointer_width()}-1 downto 0)"),
            ]

    def pointers(self):
        """The readback pointers as the control block sees them.

        They resynchronise per bit: settled and exact once the run ends, which
        is when the host reads them back; a live progress readout may tear."""
        return {
            "end_ptr_i": self.up.level(self.signal("end_ptr_host"),
                                       self.signal("end_ptr"),
                                       self.pointer_width(), numeric=True),
            "pre_head_i": self.up.level(self.signal("pre_head_host"),
                                        self.signal("pre_head"),
                                        self.depth_l2(), numeric=True),
            "pre_n_i": self.up.level(self.signal("pre_n_host"),
                                     self.signal("pre_n"),
                                     self.pointer_width(), numeric=True),
            }

    def control(self):
        port_map = {
            "clock_i": self.host.clock,
            "reset_n_i": self.host.reset_n,
            "apb_i": self.regions.control.master(),
            "apb_o": self.regions.control.slave(),
            "arm_o": self.signal("arm"),
            "abort_o": self.signal("abort"),
            "pre_lines_o": self.signal("pre_lines"),
            "max_cycles_o": self.signal("max_cycles"),
            "enable_o": "open",
            }
        port_map.update(self.status())
        port_map.update(self.pointers())
        return Instance(
            self.name("control"), self.CONTROL,
            generic_map={
                "apb_config_c": "apb_config_c",
                "depth_l2_c": self.depth_l2(),
                "fingerprint_c": "fingerprint_c",
                },
            port_map=port_map)

    def core(self):
        port_map = {
            "clock_i": self.capture.clock,
            "reset_n_i": self.capture.reset_n,
            "signals_i": self.signal("signals"),
            }
        port_map.update(self.commands())
        port_map.update({
            "trigger_i": self.trigger.tick(),
            "pre_lines_i": self.down.static(
                self.signal("pre_lines_cap"), self.signal("pre_lines"),
                self.pointer_width(), numeric=True),
            "max_cycles_i": self.down.static(
                self.signal("max_cycles_cap"), self.signal("max_cycles"),
                "32", numeric=True),
            "state_o": self.signal("state"),
            "triggered_o": self.signal("triggered"),
            "ready_o": self.signal("ready"),
            "pre_head_o": self.signal("pre_head"),
            "pre_n_o": self.signal("pre_n"),
            "end_ptr_o": self.signal("end_ptr"),
            "write_en_o": self.signal("write_en"),
            "write_addr_o": self.signal("write_addr"),
            "write_data_o": self.signal("write_data"),
            })
        return Instance(
            self.name("core"), self.CORE,
            generic_map={
                "signal_count_c": self.signal_count(),
                "depth_l2_c": self.depth_l2(),
                "count_bits_c": str(self.COUNT_BITS),
                },
            port_map=port_map)

    def control_object(self):
        # An RLE control has no length or window count: its readback is a
        # decode of the whole region from the start.
        return Expr.wrapped_call("control_desc", **self.control_arguments(
            capture_len_width="0", rle="true"))
