library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, gatecap;
use nsl_amba.axi4_stream.all;
use nsl_data.text.all;
use gatecap.axi4_stream_packer.all;

-- Self-checking bench for gatecap.axi4_stream_packer: axis_names produces the
-- expected grouping specs, and the bits axis_pack emits land where those
-- names say (host reads a downto-indexed sample, name position = bit index).
entity tb is
end entity;

architecture sim of tb is

  procedure names_check(cfg: config_t; elements: string; expected: string) is
    constant got : string := axis_names(cfg, elements);
  begin
    assert got = expected
      report "axis_names(""" & elements & """) = """ & got
        & """, expected """ & expected & """"
      severity failure;
  end procedure;

  constant link_cfg_c : config_t := config(bytes => 2, id => 3, keep => true, last => true);
  constant link_elements_c : string := "dkilvr";

begin

  check: process
    variable bus_v : bus_t;
    variable sig_v : std_ulogic_vector(axis_length(link_cfg_c, link_elements_c)-1 downto 0);
  begin
    names_check(config(bytes => 2, id => 5, dest => 6, user => 3, strobe => true, keep => true, last => true),
                "idskouvl",
                "id[0:4],data[0:15],strobe[0:1],keep[0:1],dest[0:5],user[0:2],valid,last");
    names_check(link_cfg_c, link_elements_c,
                "data[0:15],keep[0:1],id[0:2],last,valid,ready");

    -- "data[0:15],keep[0:1],id[0:2],last,valid,ready": data 0..15, keep 16..17,
    -- id 18..20, last 21, valid 22, ready 23.
    bus_v.m := transfer(link_cfg_c, value => unsigned'(x"8001"), id => "101", last => true);
    bus_v.s := accept(link_cfg_c, ready => true);
    sig_v := axis_pack(link_cfg_c, link_elements_c, bus_v);

    assert sig_v(0) = '1' report "data[0]" severity failure;
    assert sig_v(15) = '1' report "data[15]" severity failure;
    for i in 1 to 14 loop
      assert sig_v(i) = '0' report "data[" & to_string(i) & "] should be 0" severity failure;
    end loop;
    assert sig_v(16) = '1' report "keep[0]" severity failure;
    assert sig_v(17) = '1' report "keep[1]" severity failure;
    assert sig_v(18) = '1' report "id[0]" severity failure;
    assert sig_v(19) = '0' report "id[1]" severity failure;
    assert sig_v(20) = '1' report "id[2]" severity failure;
    assert sig_v(21) = '1' report "last" severity failure;
    assert sig_v(22) = '1' report "valid" severity failure;
    assert sig_v(23) = '1' report "ready" severity failure;

    report "axi4_stream_packer testbench PASSED" severity note;
    wait;
  end process;

end architecture;
