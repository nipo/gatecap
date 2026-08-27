"""The APB map inside one logic analyzer.

An instrument owns everything behind its single APB port, so the analyzer
lays its own blocks out: equal power-of-two regions selected by the address
bits just below its footprint, one per block, in a fixed order -- per domain,
in description order, the control, then the trace buffer, then the trigger the
domain hosts. A block decodes only its low address bits and never learns its
base; the descriptor's children map tells the host where each of them sits.

A region is never smaller than the twelve address bits a register file
decodes, and grows to hold the largest trace buffer, which may be far larger.
The footprint the envelope declares is the region size times the number of
regions, rounded up to a power of two by the region-index width: the entity
recomputes it and asserts it against the declared value it is handed.

Region size is an elaboration-time expression -- a buffer holding a stream
whose configuration is a generic has no size until the analyzer elaborates --
so the offsets and the footprint are expressions too.
"""

from __future__ import annotations

from dataclasses import dataclass

from acrobe_plugin.gatecap.generator import (Check, Constant,
                                             DescriptionError, Expr)


@dataclass(frozen=True)
class Block:
    """One block of the map: the name the children map exposes it under, and
    the region it sits in."""

    key: str
    index: int

    def label(self):
        return self.key.replace(".", "_")

    def offset(self):
        return f"{self.label()}_offset_c"

    def master(self):
        return f"dout_s({self.index})"

    def slave(self):
        return f"din_s({self.index})"


@dataclass(frozen=True)
class DomainBlocks:
    """The blocks of one domain, by role."""

    control: Block | None = None
    buffer: Block | None = None
    trigger: Block | None = None


class BlockLayout:
    """Equal power-of-two regions inside the instrument's own footprint."""

    # nsl_amba.address.routing_table takes at most sixteen entries.
    MAX_BLOCKS = 16
    # Every register file decodes twelve address bits, so a region is never
    # smaller than that whatever the trace buffers need.
    MIN_REGION_L2 = 12
    REGION_L2 = "region_l2_c"
    INDEX_BITS = "index_bits_c"
    MAP_SIZE_L2 = "map_size_l2_c"
    SIZE_L2 = "size_l2_c"
    APB_CONFIG = "apb_config_c"
    ROUTING_TABLE = "routing_table_c"
    MAX_EXPR = "nsl_math.arith.max"
    TABLE = "nsl_amba.address.routing_table"
    DEPS = ("nsl_amba.address", "nsl_amba.apb", "nsl_amba.apb_routing",
            "nsl_math.arith", "nsl_synthesis.assertion")

    def __init__(self, domains, path):
        self.blocks = []
        self.domains = {}
        for domain in domains:
            self.domains[domain.name] = DomainBlocks(
                control=self.__add(domain, "control", domain.captures()),
                buffer=self.__add(domain, "buffer", domain.captures()),
                trigger=self.__add(domain, "trigger", domain.hosts_trigger()))
        if len(self.blocks) > self.MAX_BLOCKS:
            raise DescriptionError(
                f"{len(self.blocks)} blocks need one APB region each, the "
                f"routing table holds {self.MAX_BLOCKS}", f"{path}.domains")

    def __add(self, domain, role, present):
        if not present:
            return None
        block = Block(f"{domain.name}.{role}", len(self.blocks))
        self.blocks.append(block)
        return block

    def of(self, name):
        return self.domains[name]

    def count(self):
        return len(self.blocks)

    def index_bits(self):
        """Address bits selecting a region: the map holds 2**index_bits of
        them, and the unused ones decode nowhere."""
        bits = 0
        while (1 << bits) < self.count():
            bits += 1
        return bits

    def declarations(self, buffer_sizes):
        """The map: region size, region-index width and footprint.

        ``buffer_sizes`` names the constants holding each trace buffer's size,
        which is what the region size has to cover."""
        size = str(self.MIN_REGION_L2)
        for name in buffer_sizes:
            size = Expr.wrapped_call(self.MAX_EXPR, size, name)
        declarations = [
            Constant(self.REGION_L2, "natural", size,
                     comment="Bytes per region, log2: enough for the largest "
                             "trace buffer, never less than a register "
                             "file's own decoding."),
            Constant(self.INDEX_BITS, "natural", str(self.index_bits()),
                     comment="Address bits selecting one of the "
                             f"{self.count()} block(s)."),
            Constant(self.MAP_SIZE_L2, "natural",
                     f"{self.REGION_L2} + {self.INDEX_BITS}",
                     comment="Bytes of address space the whole map spans, "
                             "log2. It is what the envelope declares as the "
                             "instrument's footprint."),
            ]
        return declarations

    def offsets(self):
        """Where each block sits inside the analyzer's own segment: only the
        descriptor needs them."""
        return [Constant(block.offset(), "natural",
                         f"{block.index} * 2**{self.REGION_L2}")
                for block in self.blocks]

    def routing_table(self):
        return Constant(
            self.ROUTING_TABLE, "nsl_amba.address.address_vector",
            self.__table(),
            comment="One prefix per region, in block order. The bits above "
                    "the instrument's footprint are the backplane's business "
                    "and stay don't-care.")

    def __table(self):
        width = f"{self.APB_CONFIG}.address_width"
        arguments = ",\n".join(
            f"  block_prefix({width}, {self.MAP_SIZE_L2}, "
            f"{self.INDEX_BITS}, {block.index})"
            for block in self.blocks)
        return f"{self.TABLE}({width},\n{arguments})"

    def checks(self):
        return [
            Check("footprint_check",
                  "the declared footprint must match the map the analyzer "
                  "decodes",
                  f"{self.SIZE_L2} = {self.MAP_SIZE_L2}"),
            Check("address_width_check",
                  "the APB configuration must cover the analyzer's footprint",
                  f"{self.APB_CONFIG}.address_width >= {self.MAP_SIZE_L2}"),
            ]

    PREFIX_FUNCTION = """\
-- A `width`-bit routing prefix selecting region `index` of 2**index_bits
-- equal regions inside a `size_l2`-byte footprint: the address bits above
-- the footprint belong to whoever placed the instrument and stay
-- don't-care.
function block_prefix(width, size_l2, index_bits, index : natural)
  return string is
  variable s : string(1 to width);
  variable rest : natural := index;
begin
  s := (others => '-');
  for i in index_bits downto 1 loop
    s(width - size_l2 + i) := character'val(character'pos('0') + rest mod 2);
    rest := rest / 2;
  end loop;
  return s;
end function;"""
