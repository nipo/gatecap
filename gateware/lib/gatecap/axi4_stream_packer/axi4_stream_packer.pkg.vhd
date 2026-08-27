library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;

-- Pack an AXI4-Stream bus into a gatecap capture probe vector, and name the
-- bits so the host reconstructs the stream.
--
-- Layout is gatecap-native: elements are laid out low bits first in the
-- order given, each field ascending (bit/byte k at the k-th position), data
-- little-endian (data[0] is the value LSB, keep[0]/strobe[0] pair with data
-- byte 0). axis_pack and axis_names walk the same list the same way, so
-- the vector and the names always agree.
--
-- elements is a string over "idskouvlr": id, data, strobe, keep, dest ('o'),
-- user, valid, last, ready. Fields absent from cfg (zero width, or the
-- has_* flag clear) contribute nothing and are named nothing.
package axi4_stream_packer is

  function axis_length(cfg: nsl_amba.axi4_stream.config_t;
                        elements: string) return natural;

  function axis_pack(cfg: nsl_amba.axi4_stream.config_t;
                      elements: string;
                      b: nsl_amba.axi4_stream.bus_t) return std_ulogic_vector;

  function axis_names(cfg: nsl_amba.axi4_stream.config_t;
                       elements: string) return string;

end package;

library nsl_amba, nsl_logic, nsl_data;
use nsl_amba.axi4_stream.all;
use nsl_logic.bool.all;
use nsl_data.text.all;

package body axi4_stream_packer is

  function axis_length(cfg: config_t;
                        elements: string) return natural
  is
    variable ret : natural := 0;
  begin
    for ei in elements'range loop
      case elements(ei) is
        when 'i' => ret := ret + cfg.id_width;
        when 'd' => ret := ret + cfg.data_width * 8;
        when 's' => if cfg.has_strobe then ret := ret + cfg.data_width; end if;
        when 'k' => if cfg.has_keep then ret := ret + cfg.data_width; end if;
        when 'o' => ret := ret + cfg.dest_width;
        when 'u' => ret := ret + cfg.user_width;
        when 'v' => ret := ret + 1;
        when 'l' => if cfg.has_last then ret := ret + 1; end if;
        when 'r' => if cfg.has_ready then ret := ret + 1; end if;
        when others =>
          assert false
            report "Bad key, must be one of [idskouvlr]"
            severity failure;
      end case;
    end loop;
    return ret;
  end function;

  function axis_pack(cfg: config_t;
                      elements: string;
                      b: bus_t) return std_ulogic_vector
  is
    constant s : natural := axis_length(cfg, elements);
    variable ret : std_ulogic_vector(s-1 downto 0) := (others => '0');
    variable point : natural := 0;
    alias m : master_t is b.m;
    variable dvec : unsigned(cfg.data_width*8-1 downto 0);
  begin
    for ei in elements'range loop
      case elements(ei) is
        when 'i' =>
          for k in 0 to cfg.id_width-1 loop
            ret(point+k) := m.id(k);
          end loop;
          point := point + cfg.id_width;
        when 'd' =>
          dvec := value(cfg, m);
          for k in 0 to cfg.data_width*8-1 loop
            ret(point+k) := dvec(k);
          end loop;
          point := point + cfg.data_width * 8;
        when 's' =>
          if cfg.has_strobe then
            for k in 0 to cfg.data_width-1 loop
              ret(point+k) := m.strobe(k);
            end loop;
            point := point + cfg.data_width;
          end if;
        when 'k' =>
          if cfg.has_keep then
            for k in 0 to cfg.data_width-1 loop
              ret(point+k) := m.keep(k);
            end loop;
            point := point + cfg.data_width;
          end if;
        when 'o' =>
          for k in 0 to cfg.dest_width-1 loop
            ret(point+k) := m.dest(k);
          end loop;
          point := point + cfg.dest_width;
        when 'u' =>
          for k in 0 to cfg.user_width-1 loop
            ret(point+k) := m.user(k);
          end loop;
          point := point + cfg.user_width;
        when 'v' =>
          ret(point) := to_logic(is_valid(cfg, m));
          point := point + 1;
        when 'l' =>
          if cfg.has_last then
            ret(point) := to_logic(is_last(cfg, m));
            point := point + 1;
          end if;
        when 'r' =>
          if cfg.has_ready then
            ret(point) := to_logic(is_ready(cfg, b.s));
            point := point + 1;
          end if;
        when others =>
          assert false
            report "Bad key, must be one of [idskouvlr]"
            severity failure;
      end case;
    end loop;

    assert point = s
      report "Final size does not match vector. Using a key twice ?"
      severity failure;

    return ret;
  end function;

  function axis_names(cfg: config_t;
                       elements: string) return string
  is
    function ranged(name: string; hi: integer) return string is
    begin
      return name & "[0:" & to_string(hi) & "]";
    end function;

    function frag(c: character) return string is
    begin
      case c is
        when 'i' =>
          if cfg.id_width > 0 then return ranged("id", cfg.id_width-1); end if;
        when 'd' =>
          if cfg.data_width > 0 then return ranged("data", cfg.data_width*8-1); end if;
        when 's' =>
          if cfg.has_strobe and cfg.data_width > 0 then return ranged("strobe", cfg.data_width-1); end if;
        when 'k' =>
          if cfg.has_keep and cfg.data_width > 0 then return ranged("keep", cfg.data_width-1); end if;
        when 'o' =>
          if cfg.dest_width > 0 then return ranged("dest", cfg.dest_width-1); end if;
        when 'u' =>
          if cfg.user_width > 0 then return ranged("user", cfg.user_width-1); end if;
        when 'v' =>
          return "valid";
        when 'l' =>
          if cfg.has_last then return "last"; end if;
        when 'r' =>
          if cfg.has_ready then return "ready"; end if;
        when others =>
          assert false
            report "Bad key, must be one of [idskouvlr]"
            severity failure;
      end case;
      return "";
    end function;

    function combine(a, b: string) return string is
    begin
      if a'length = 0 then return b; end if;
      if b'length = 0 then return a; end if;
      return a & "," & b;
    end function;

    -- Fields in elements order (low bit first), skipping empty fragments.
    function joined(idx: integer) return string is
    begin
      if idx > elements'high then
        return "";
      end if;
      return combine(frag(elements(idx)), joined(idx+1));
    end function;
  begin
    return joined(elements'low);
  end function;

end package body;
