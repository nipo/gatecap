library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_bnoc;

-- Pack a bnoc variant bus into a gatecap capture probe vector, and
-- name the bits so the host reconstructs the stream.
package bnoc_packer is

  function pipe_length(elements: string := "dvr") return natural;
  function pipe_pack(b: nsl_bnoc.pipe.pipe_bus_t;
                     elements: string := "dvr") return std_ulogic_vector;
  function pipe_names(elements: string := "dvr") return string;

  function framed_length(elements: string := "dvlr") return natural;
  function framed_pack(b: nsl_bnoc.framed.framed_bus_t;
                       elements: string := "dvlr") return std_ulogic_vector;
  function framed_names(elements: string := "dvlr") return string;

end package;

package body bnoc_packer is

  function pipe_length(elements: string := "dvr") return natural
  is
    variable ret : natural := 0;
  begin
    for ei in elements'range loop
      case elements(ei) is
        when 'd' => ret := ret + 8;
        when 'v' => ret := ret + 1;
        when 'r' => ret := ret + 1;
        when others =>
          assert false
            report "Bad key, must be one of [dvr]"
            severity failure;
      end case;
    end loop;
    return ret;
  end function;

  function pipe_pack(b: nsl_bnoc.pipe.pipe_bus_t;
                     elements: string := "dvr") return std_ulogic_vector
  is
    constant s : natural := pipe_length(elements);
    variable ret : std_ulogic_vector(s-1 downto 0) := (others => '0');
    variable point : natural := 0;
  begin
    for ei in elements'range loop
      case elements(ei) is
        when 'd' =>
          ret(point+7 downto point) := b.req.data;
          point := point + 8;

        when 'v' =>
          ret(point) := b.req.valid;
          point := point + 1;
          
        when 'r' =>
          ret(point) := b.ack.ready;
          point := point + 1;

        when others =>
          assert false
            report "Bad key, must be one of [dvr]"
            severity failure;
      end case;
    end loop;

    assert point = s
      report "Final size does not match vector. Using a key twice ?"
      severity failure;

    return ret;
  end function;

  function pipe_names(elements: string := "dvr") return string
  is
    alias xe: string(2 to elements'length+1) is elements;
    function frag(c: character) return string is
    begin
      case c is
        when 'd' =>
          return "data[0:7]";
        when 'v' =>
          return "valid";
        when 'r' =>
          return "ready";
        when others =>
          assert false
            report "Bad key, must be one of [dvr]"
            severity failure;
      end case;
      return "";
    end function;

    function append_next(elements: string) return string
    is
    begin
      if elements'length = 0 then
        return "";
      elsif elements'length = 1 then
        return frag(elements(elements'left));
      else
        return frag(elements(elements'left))
          & "," & append_next(elements(elements'left+1 to elements'right));
      end if;
    end function;
  begin
    return append_next(xe);
  end function;

  function framed_length(elements: string := "dvlr") return natural
  is
    variable ret : natural := 0;
  begin
    for ei in elements'range loop
      case elements(ei) is
        when 'd' => ret := ret + 8;
        when 'v' => ret := ret + 1;
        when 'l' => ret := ret + 1;
        when 'r' => ret := ret + 1;
        when others =>
          assert false
            report "Bad key, must be one of [dvlr]"
            severity failure;
      end case;
    end loop;
    return ret;
  end function;

  function framed_pack(b: nsl_bnoc.framed.framed_bus_t;
                       elements: string := "dvlr") return std_ulogic_vector
  is
    constant s : natural := framed_length(elements);
    variable ret : std_ulogic_vector(s-1 downto 0) := (others => '0');
    variable point : natural := 0;
  begin
    for ei in elements'range loop
      case elements(ei) is
        when 'd' =>
          ret(point+7 downto point) := b.req.data;
          point := point + 8;

        when 'v' =>
          ret(point) := b.req.valid;
          point := point + 1;
          
        when 'l' =>
          ret(point) := b.req.last;
          point := point + 1;

        when 'r' =>
          ret(point) := b.ack.ready;
          point := point + 1;

        when others =>
          assert false
            report "Bad key, must be one of [dvlr]"
            severity failure;
      end case;
    end loop;

    assert point = s
      report "Final size does not match vector. Using a key twice ?"
      severity failure;

    return ret;
  end function;

  function framed_names(elements: string := "dvlr") return string
  is
    alias xe: string(2 to elements'length+1) is elements;
    function frag(c: character) return string is
    begin
      case c is
        when 'd' =>
          return "data[0:7]";
        when 'v' =>
          return "valid";
        when 'l' =>
          return "last";
        when 'r' =>
          return "ready";
        when others =>
          assert false
            report "Bad key, must be one of [dvlr]"
            severity failure;
      end case;
      return "";
    end function;

    function append_next(elements: string) return string is
    begin
      if elements'length = 0 then
        return "";
      elsif elements'length = 1 then
        return frag(elements(elements'left));
      else
        return frag(elements(elements'left))
          & "," & append_next(elements(elements'left+1 to elements'right));
      end if;
    end function;
  begin
    return append_next(xe);
  end function;

end package body;
