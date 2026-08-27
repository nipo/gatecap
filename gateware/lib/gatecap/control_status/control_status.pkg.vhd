library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_math;

-- Control/status instrument: a front panel of plain wires. Four signal
-- kinds, each named and directed in the user design:
--
--   control   host-written level driven to an output, read back
--   status    input level, sampled continuously
--   tick out  host-strobed event, one instrument-clock cycle wide
--   tick in   input event; owns a sticky bit and a free-running counter
--
-- The instrument is split in two entities because the host clock is not
-- permanent (a JTAG TCK only runs while the probe drives it), while events
-- must never be missed:
--
--   control_status_shell  APB register file, host clock domain
--   control_status_core   event logic, instrument clock domain
--
-- Neither entity contains a clock crossing. The assembler wires shell to
-- core, inserting the crossings below when the two clocks differ and plain
-- wires when they are the same, so the same-clock case costs nothing.
--
-- Inner contract, shell port -> core port, and the crossing to insert:
--
--   control_o             -> (user)               interdomain_reg, one per
--                                                 word, whole word at once so
--                                                 the value never tears
--   status_i              <- status_o             interdomain_reg, one per word
--   tick_out_mask_o       -> tick_out_mask_i      interdomain_static_reg
--   tick_out_strobe_o     -> tick_out_strobe_i    interdomain_tick
--   sticky_clear_o        -> sticky_clear_i       interdomain_static_reg
--   sticky_clear_strobe_o -> sticky_clear_strobe_i interdomain_tick
--   sticky_i              <- sticky_o             interdomain_reg, one per word
--   counter_i             <- counter_o            interdomain_counter, one per
--                                                 counter, binary at both
--                                                 ends, tick_counter_width_c
--                                                 bits (see below)
--
-- Controls do not reach the core: they are stored in the shell and cross
-- straight to the user logic.
--
-- The mask-plus-strobe pairs rely on the tick crossing being slower than the
-- static register settling, so the mask is stable when the strobe lands.
-- The shell zeroes every other word of a mask when it writes one, hence one
-- strobe serves all words of a kind and a strobe never re-fires a stale word.
--
-- Counters are exchanged as whole words for uniformity, but only bits
-- tick_counter_width_c-1 downto 0 are meaningful and only those may be
-- crossed: they wrap, and a wrap is a single step of the gray code only at
-- that width. Crossing the zero-extended word would break the one-step
-- invariant interdomain_counter needs.
package control_status is

  -- The core boundary is arrays of full 32-bit words with per-element width
  -- generics: VHDL-93 has no array of unconstrained arrays. The generated
  -- rack slices each word down to its declared width. Unused high bits of
  -- inputs are ignored, unused high bits of outputs are driven low.
  subtype panel_word_t is std_ulogic_vector(31 downto 0);
  type panel_word_vector is array (natural range <>) of panel_word_t;

  -- A kind with no signal at all: the generator emits instances without
  -- ticks or without controls.
  constant no_panel_signals_c : nsl_math.int_ext.integer_vector(0 to -1)
    := (others => 0);

  -- Element /index/ (0-based) of a boundary generic. The vectors are used
  -- position-wise, whatever their declared index range.
  function panel_width(v : nsl_math.int_ext.integer_vector;
                       index : natural) return natural;

  -- Total bit count of a boundary generic, and the count preceding element
  -- /index/. Tick inputs are numbered this way: word-major, and the counter
  -- of bit /b/ of word /g/ is counter number panel_bit_base(v, g) + b.
  function panel_bit_total(v : nsl_math.int_ext.integer_vector) return natural;
  function panel_bit_base(v : nsl_math.int_ext.integer_vector;
                          index : natural) return natural;

  -- Every element in 1 to 32, and an ascending index range.
  function panel_widths_legal(v : nsl_math.int_ext.integer_vector) return boolean;

  -- /value/ in a full word, bits at and above /width/ cleared.
  function panel_masked(value : unsigned; width : natural) return panel_word_t;

  -- Host-domain APB register file. Register map (word offsets within each
  -- 0x100-stride region, NTO = tick-out words, NTI = tick-in words,
  -- NS = status words, NC = panel_bit_total(tick_in_count_c)):
  --
  --   0x000 action, write-only, reads as zero
  --     +0                        tick-out strobe word 0 .. NTO-1: a write
  --                               fires the ticks set in the written value
  --     +NTO                      sticky-clear word 0 .. NTI-1: write 1 to
  --                               clear
  --     +NTO+NTI                  counter-clear word 0 .. NTI-1: write 1 to
  --                               rebase that tick input's counter to zero
  --   0x100 config
  --     empty, reserved
  --   0x200 status, read-only
  --     +0                        STATUS, reserved, reads as zero
  --     +1                        FINGERPRINT
  --     +2                        sticky word 0 .. NTI-1
  --     +2+NTI                    status word 0 .. NS-1
  --     +2+NTI+NS                 counter 0 .. NC-1
  --   0x300 arrays, read/write
  --     +0                        control word 0 .. control words-1
  --
  -- The whole live state of the panel is one contiguous read-only run, so a
  -- single burst status poll carries it.
  component control_status_shell is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      control_width_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      status_width_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      tick_out_count_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      tick_in_count_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      tick_counter_width_c : natural range 1 to 32 := 32;
      fingerprint_c : unsigned(31 downto 0) := (others => '0')
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      control_o : out panel_word_vector(0 to control_width_c'length-1);

      status_i : in panel_word_vector(0 to status_width_c'length-1);

      tick_out_mask_o : out panel_word_vector(0 to tick_out_count_c'length-1);
      tick_out_strobe_o : out std_ulogic;

      sticky_clear_o : out panel_word_vector(0 to tick_in_count_c'length-1);
      sticky_clear_strobe_o : out std_ulogic;
      sticky_i : in panel_word_vector(0 to tick_in_count_c'length-1);

      counter_i : in panel_word_vector(0 to panel_bit_total(tick_in_count_c)-1)
      );
  end component;

  -- Instrument-domain event logic. Everything that must not miss an event
  -- lives here; the shell only ever reads levels out of it.
  component control_status_core is
    generic (
      status_width_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      tick_out_count_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      tick_in_count_c : nsl_math.int_ext.integer_vector := no_panel_signals_c;
      tick_counter_width_c : natural range 1 to 32 := 32
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      status_i : in panel_word_vector(0 to status_width_c'length-1);
      status_o : out panel_word_vector(0 to status_width_c'length-1);

      tick_out_mask_i : in panel_word_vector(0 to tick_out_count_c'length-1);
      tick_out_strobe_i : in std_ulogic;
      tick_out_o : out panel_word_vector(0 to tick_out_count_c'length-1);

      tick_in_i : in panel_word_vector(0 to tick_in_count_c'length-1);
      sticky_clear_i : in panel_word_vector(0 to tick_in_count_c'length-1);
      sticky_clear_strobe_i : in std_ulogic;
      sticky_o : out panel_word_vector(0 to tick_in_count_c'length-1);

      counter_o : out panel_word_vector(0 to panel_bit_total(tick_in_count_c)-1)
      );
  end component;

end package;

package body control_status is

  function panel_width(v : nsl_math.int_ext.integer_vector;
                       index : natural) return natural
  is
  begin
    assert v'ascending
      report "panel boundary generics are used position-wise and must be ascending"
      severity failure;
    return v(v'low + index);
  end function;

  function panel_bit_total(v : nsl_math.int_ext.integer_vector) return natural
  is
    variable total : natural := 0;
  begin
    for i in v'range loop
      total := total + v(i);
    end loop;
    return total;
  end function;

  function panel_bit_base(v : nsl_math.int_ext.integer_vector;
                          index : natural) return natural
  is
    variable total : natural := 0;
  begin
    for i in 0 to index-1 loop
      total := total + panel_width(v, i);
    end loop;
    return total;
  end function;

  function panel_widths_legal(v : nsl_math.int_ext.integer_vector) return boolean
  is
  begin
    if v'length /= 0 and not v'ascending then
      return false;
    end if;
    for i in v'range loop
      if v(i) < 1 or v(i) > 32 then
        return false;
      end if;
    end loop;
    return true;
  end function;

  function panel_masked(value : unsigned; width : natural) return panel_word_t
  is
    variable ret : panel_word_t := (others => '0');
    variable ext : unsigned(31 downto 0);
  begin
    ext := resize(value, 32);
    for i in 0 to width-1 loop
      ret(i) := ext(i);
    end loop;
    return ret;
  end function;

end package body;
