library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_math, nsl_synthesis, gatecap;
use nsl_math.int_ext.all;
use gatecap.control_status.all;

-- Instrument-domain half of the control/status instrument. Its clock is
-- permanent, unlike the host clock, so nothing here can miss an event:
--
--   tick out   one registered cycle of (strobe and mask); every tick of a
--              word asserts in the same cycle
--   sticky     set by an incoming event, cleared by the crossed clear mask
--              and strobe, set winning over a concurrent clear
--   counter    one free-running wrapping counter per tick input, stepping by
--              at most 1 per cycle so interdomain_counter can cross it
--   status     input levels sampled, masked to their declared width
--
-- It contains no clock crossing: the assembler wires it to the shell (see
-- the control_status package for the contract).
entity control_status_core is
  generic (
    status_width_c : integer_vector := no_panel_signals_c;
    tick_out_count_c : integer_vector := no_panel_signals_c;
    tick_in_count_c : integer_vector := no_panel_signals_c;
    tick_counter_width_c : natural range 1 to 32 := 32
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    -- From user logic, to the shell through one interdomain_reg per word.
    status_i : in panel_word_vector(0 to status_width_c'length-1);
    status_o : out panel_word_vector(0 to status_width_c'length-1);

    -- From the shell.
    tick_out_mask_i : in panel_word_vector(0 to tick_out_count_c'length-1);
    tick_out_strobe_i : in std_ulogic;
    -- To user logic, one cycle wide.
    tick_out_o : out panel_word_vector(0 to tick_out_count_c'length-1);

    -- From user logic, one cycle wide.
    tick_in_i : in panel_word_vector(0 to tick_in_count_c'length-1);

    -- From the shell.
    sticky_clear_i : in panel_word_vector(0 to tick_in_count_c'length-1);
    sticky_clear_strobe_i : in std_ulogic;
    -- To the shell.
    sticky_o : out panel_word_vector(0 to tick_in_count_c'length-1);

    -- To the shell, one counter per tick input, word-major, meaningful (and
    -- crossable) on bits tick_counter_width_c-1 downto 0 only.
    counter_o : out panel_word_vector(0 to panel_bit_total(tick_in_count_c)-1)
    );
end entity;

architecture rtl of control_status_core is

  constant status_count_c : natural := status_width_c'length;
  constant tick_out_words_c : natural := tick_out_count_c'length;
  constant tick_in_words_c : natural := tick_in_count_c'length;
  constant counter_count_c : natural := panel_bit_total(tick_in_count_c);

  constant widths_legal_c : boolean :=
    panel_widths_legal(status_width_c)
    and panel_widths_legal(tick_out_count_c)
    and panel_widths_legal(tick_in_count_c);

  signal status_s : panel_word_vector(0 to status_count_c-1);
  signal tick_out_s : panel_word_vector(0 to tick_out_words_c-1);
  signal sticky_s : panel_word_vector(0 to tick_in_words_c-1);

  subtype counter_t is unsigned(tick_counter_width_c-1 downto 0);
  type counter_vector is array (natural range <>) of counter_t;
  signal counter_s : counter_vector(0 to counter_count_c-1);

begin

  assert widths_legal_c
    report "control/status boundary widths must all be in 1 to 32"
    severity failure;

  widths_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "control/status boundary widths must all be in 1 to 32",
      condition_c => widths_legal_c
      )
    port map(
      unused_i => '0'
      );

  regs: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      for k in 0 to status_count_c-1 loop
        status_s(k) <= panel_masked(unsigned(status_i(k)),
                                    panel_width(status_width_c, k));
      end loop;

      -- The mask is stable by the time the strobe crosses, so one registered
      -- AND gives every tick of the word the same single cycle.
      for g in 0 to tick_out_words_c-1 loop
        tick_out_s(g) <= (others => '0');
        for b in 0 to panel_width(tick_out_count_c, g)-1 loop
          tick_out_s(g)(b) <= tick_out_strobe_i and tick_out_mask_i(g)(b);
        end loop;
      end loop;

      for g in 0 to tick_in_words_c-1 loop
        for b in 0 to panel_width(tick_in_count_c, g)-1 loop
          if sticky_clear_strobe_i = '1' and sticky_clear_i(g)(b) = '1' then
            sticky_s(g)(b) <= '0';
          end if;
          -- Set last: an event landing on the clear cycle wins.
          if tick_in_i(g)(b) = '1' then
            sticky_s(g)(b) <= '1';
            counter_s(panel_bit_base(tick_in_count_c, g) + b)
              <= counter_s(panel_bit_base(tick_in_count_c, g) + b) + 1;
          end if;
        end loop;
      end loop;
    end if;

    if reset_n_i = '0' then
      status_s <= (others => (others => '0'));
      tick_out_s <= (others => (others => '0'));
      sticky_s <= (others => (others => '0'));
      counter_s <= (others => (others => '0'));
    end if;
  end process;

  status_o <= status_s;
  tick_out_o <= tick_out_s;
  sticky_o <= sticky_s;

  counter_out: for i in 0 to counter_count_c-1 generate
    counter_o(i) <= std_ulogic_vector(resize(counter_s(i), 32));
  end generate;

end architecture;
