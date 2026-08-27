"""Golden fragments of the generator's VHDL code model.

Run: python3.13 -m pytest host/tests/test_generator_vhdl.py
"""

import pytest

from acrobe_plugin.gatecap.generator.vhdl import (Architecture, Assignment,
                                                  Blank, Comment, Constant,
                                                  DesignFile, Emitter, Entity,
                                                  Expr, Generic, Identifier,
                                                  Instance, LibraryClause,
                                                  Package, Port, Process,
                                                  RawStatement, SignalDecl,
                                                  UseClause)


def test_identifier_rules():
    assert Identifier.legal("control_clock")
    assert Identifier.rejection("1st") == "must start with a letter"
    assert Identifier.rejection("a__b") == "must not contain two consecutive underscores"
    assert Identifier.rejection("a_") == "must not end with an underscore"
    assert Identifier.rejection("Entity") == "is a VHDL reserved word"
    assert Identifier.rejection("a-b") == "illegal character '-'"
    assert Identifier.rejection("") == "must be a non-empty string"


def test_expression_helpers():
    assert Expr.string('say "hi"') == '"say ""hi"""'
    assert Expr.call("f", "a", "b", key="v") == "f(a, b, key => v)"
    assert Expr.concat('"a"', "", "f(x)") == '"a" & f(x)'
    assert Expr.slice_down("v_s", "7", "0") == "v_s(7 downto 0)"
    assert Expr.scalar_vector("k_i") == "std_ulogic_vector'(0 => k_i)"
    assert Expr.boolean(False) == "false"
    # An expression is a plain string, so composition needs nothing else.
    assert Expr.call("axis_length", "ctrl_command_config_c",
                     Expr.string("vlr")) == \
        'axis_length(ctrl_command_config_c, "vlr")'


def test_entity_rendering():
    entity = Entity(
        "my_capture",
        generics=(Generic("stream_config_c", "nsl_amba.axi4_stream.config_t"),
                  Generic("window_count_c", "natural", "1",
                          comment="Windows per run.")),
        ports=(Port("clock_i", "in", "std_ulogic"),
               Port("state_i", "in", "std_ulogic_vector(1 downto 0)"),
               Port("apb_o", "out", "nsl_amba.apb.slave_t")))
    assert Emitter.render(entity) == """\
entity my_capture is
  generic (
    stream_config_c : nsl_amba.axi4_stream.config_t;
    -- Windows per run.
    window_count_c : natural := 1
    );
  port (
    clock_i : in std_ulogic;
    state_i : in std_ulogic_vector(1 downto 0);
    apb_o : out nsl_amba.apb.slave_t
    );
end entity;
"""


def test_component_declaration_mirrors_the_entity():
    entity = Entity("core", generics=(Generic("depth_l2_c", "natural"),),
                    ports=(Port("clock_i", "in", "std_ulogic"),))
    assert Emitter.render(Package("my_pkg", (entity.component(),))) == """\
package my_pkg is

  component core is
    generic (
      depth_l2_c : natural
      );
    port (
      clock_i : in std_ulogic
      );
  end component;

end package;
"""


def test_architecture_rendering():
    architecture = Architecture(
        "rtl", "my_capture",
        declarations=(
            Constant("signal_count_c", "natural",
                     Expr.call("gatecap.axi4_stream_packer.axis_length",
                               "control_command_config_c", '"idskouvlr"')),
            SignalDecl("trigger_s", "std_ulogic"),
            SignalDecl("signals_s", "std_ulogic_vector(signal_count_c-1 downto 0)",
                       comment="Capture vector, low bits first."),
            ),
        statements=(
            Assignment("signals_s", "gatecap.axi4_stream_packer.axis_pack("
                       'control_command_config_c, "idskouvlr", control_command_i)'),
            Instance("control", "gatecap.control.capture_control",
                     generic_map={"depth_l2_c": "12"},
                     port_map={"clock_i": "control_clock_i",
                               "trigger_o": "trigger_s"}),
            Instance("dispatch", "nsl_amba.apb_routing.apb_dispatch",
                     port_map={"clock_i": "control_clock_i"}),
            ))
    assert Emitter.render(architecture) == """\
architecture rtl of my_capture is

  constant signal_count_c : natural := \
gatecap.axi4_stream_packer.axis_length(control_command_config_c, "idskouvlr");

  signal trigger_s : std_ulogic;

  -- Capture vector, low bits first.
  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0);

begin

  signals_s <= gatecap.axi4_stream_packer.axis_pack\
(control_command_config_c, "idskouvlr", control_command_i);

  control: gatecap.control.capture_control
    generic map(
      depth_l2_c => 12
      )
    port map(
      clock_i => control_clock_i,
      trigger_o => trigger_s
      );

  dispatch: nsl_amba.apb_routing.apb_dispatch
    port map(
      clock_i => control_clock_i
      );

end architecture;
"""


def test_multi_line_constant_keeps_its_layout():
    constant = Constant("identify_c", "byte_string",
                        "bridge_identify(\n"
                        "  addr_bits => 14,\n"
                        "  descriptor_base => 0)")
    assert Emitter.render(constant) == """\
constant identify_c : byte_string :=
  bridge_identify(
    addr_bits => 14,
    descriptor_base => 0);
"""


def test_process_carries_its_body_as_text():
    process = Process("head_hold", ("capture_clock_i",),
                      "if rising_edge(capture_clock_i) then\n"
                      "  if head_we_s = '1' then\n"
                      "    head_hold_s <= head_s;\n"
                      "  end if;\n"
                      "end if;")
    assert Emitter.render(process) == """\
head_hold: process(capture_clock_i)
begin
  if rising_edge(capture_clock_i) then
    if head_we_s = '1' then
      head_hold_s <= head_s;
    end if;
  end if;
end process;
"""


def test_design_file_context_clauses():
    assert DesignFile.libraries_of(("nsl_amba.apb", "gatecap.control",
                                    "nsl_amba.axi4_stream")) == \
        ("nsl_amba", "gatecap")
    rendered = DesignFile(
        header="Generated by acrobe gatecap generate -- do not edit.",
        clauses=DesignFile.context(("nsl_amba", "gatecap"),
                                   ("nsl_amba.apb.all",)),
        units=(Entity("e", ports=(Port("clock_i", "in", "std_ulogic"),)),)
        ).render()
    assert rendered == """\
-- Generated by acrobe gatecap generate -- do not edit.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, gatecap;
use nsl_amba.apb.all;

entity e is
  port (
    clock_i : in std_ulogic
    );
end entity;
"""


def test_rendering_is_deterministic():
    def build():
        return DesignFile(
            clauses=(LibraryClause(("nsl_amba",)), UseClause("nsl_amba.apb.all"),
                     Blank(), Comment("map")),
            units=(Architecture("rtl", "e", statements=(
                RawStatement("assert true report \"ok\" severity note;"),)),)
            ).render()
    assert build() == build()


def test_emitter_rejects_indentation_underflow():
    with pytest.raises(AssertionError):
        Emitter().pop()
