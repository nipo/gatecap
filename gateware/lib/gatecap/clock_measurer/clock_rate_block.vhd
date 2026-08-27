library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library gatecap, nsl_amba, nsl_clocking, nsl_synthesis;
use gatecap.clock_measurer.all;

-- Rate of every observed clock, measured against one reference clock and
-- published in an APB rate array.
--
-- Three clock domains meet here. Each observed clock drives a free-running
-- counter inside its own measurer; the measurer resynchronises that counter
-- into the reference domain and differentiates it over a fixed window, so
-- rate_hz_o is a reference-domain value refreshed 2**update_hz_l2_c times per
-- second. The register file runs on the host clock, which is unrelated to the
-- reference, hence the one crossing this block owns: the whole rate set at
-- once, from the reference domain to the host domain.
--
-- Every clock reaches its consumer as a port, straight into the instance that
-- uses it: a clock passed through a signal assignment would gain a delta
-- cycle, and here it would count against a time base skewed from itself.
entity clock_rate_block is
  generic (
    apb_config_c : nsl_amba.apb.config_t;
    size_l2_c : natural;
    measured_count_c : natural;
    reference_hz_c : natural;
    rate_width_c : natural;
    update_hz_l2_c : natural := 0;
    fingerprint_c : unsigned(31 downto 0) := (others => '0')
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    apb_i : in nsl_amba.apb.master_t;
    apb_o : out nsl_amba.apb.slave_t;

    reference_clock_i : in std_ulogic;

    measured_clock_i : in std_ulogic_vector(measured_count_c-1 downto 0)
    );
end entity;

architecture rtl of clock_rate_block is

  -- Words the register file decodes: the gatecap groups end at the array one,
  -- so 0x300 and above is what is left for the rates. The instrument's
  -- envelope declares a footprint derived from this same constant.
  constant reg_count_l2_c : natural := CLOCK_MEASURER_REG_COUNT_L2_C;

  constant REG_STATUS_C : natural := 16#200# / 4;
  constant REG_FINGERPRINT_C : natural := REG_STATUS_C + 1;
  constant REG_RATE_C : natural := 16#300# / 4;

  -- Rate registers the array group can hold.
  constant rate_slot_count_c : natural := 2**reg_count_l2_c - REG_RATE_C;

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  subtype rate_t is unsigned(rate_width_c-1 downto 0);
  type rate_vector_t is array(natural range <>) of rate_t;

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  -- Reference domain: one rate per measurer, and the same set flattened for
  -- the crossing.
  signal rate_s : rate_vector_t(0 to measured_count_c-1);
  signal rate_flat_s : std_ulogic_vector(measured_count_c*rate_width_c-1 downto 0);

  -- Host domain: what the register file reads.
  signal rate_host_s : std_ulogic_vector(measured_count_c*rate_width_c-1 downto 0);

  -- Host reset, asserted asynchronously and released on a reference edge.
  signal reference_reset_n_s : std_ulogic;

begin

  -- The rack allocates a segment from the envelope's declared footprint and
  -- routes to it by prefix, so a block decoding more than it declared would
  -- answer inside a neighbour's segment.
  assert size_l2_c
    = clock_measurer_size_l2(apb_config_c.data_bus_width_l2)
    report "the declared address footprint does not match the decoding"
    severity failure;

  size_l2_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "the declared address footprint does not match the "
        & "decoding",
      condition_c => size_l2_c
        = clock_measurer_size_l2(apb_config_c.data_bus_width_l2)
      )
    port map(
      unused_i => '0'
      );

  assert measured_count_c >= 1 and measured_count_c <= rate_slot_count_c
    report "a clock measurer watches between 1 and "
      & integer'image(rate_slot_count_c) & " clocks"
    severity failure;

  measured_count_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "a clock measurer watches between 1 and "
        & integer'image(rate_slot_count_c) & " clocks",
      condition_c => measured_count_c >= 1
        and measured_count_c <= rate_slot_count_c
      )
    port map(
      unused_i => '0'
      );

  assert rate_width_c <= word_bits_c
    report "a measured rate must fit one APB word"
    severity failure;

  rate_width_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "a measured rate must fit one APB word",
      condition_c => rate_width_c <= word_bits_c
      )
    port map(
      unused_i => '0'
      );

  -- A measurer holds its count in the rate bits above update_hz_l2_c and
  -- zeroes the ones below, so a rate narrower than the quantum has no bits
  -- left to count in.
  assert rate_width_c > update_hz_l2_c
    report "the rate width must exceed the update rate's log2"
    severity failure;

  rate_quantum_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "the rate width must exceed the update rate's log2",
      condition_c => rate_width_c > update_hz_l2_c
      )
    port map(
      unused_i => '0'
      );

  -- The window is reference_hz_c / 2**update_hz_l2_c reference cycles, and a
  -- window of no cycles measures nothing.
  assert reference_hz_c / 2**update_hz_l2_c >= 2
    report "the measurement window must span at least two reference cycles"
    severity failure;

  window_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "the measurement window must span at least two reference "
        & "cycles",
      condition_c => reference_hz_c / 2**update_hz_l2_c >= 2
      )
    port map(
      unused_i => '0'
      );

  -- The measurers are the only thing in the reference domain, and they need a
  -- reset that releases in it. Deriving it from the host reset keeps the
  -- instrument's port list to clocks alone.
  reference_reset: nsl_clocking.async.async_edge
    generic map(
      cycle_count_c => 2,
      target_value_c => '1',
      async_reset_c => true
      )
    port map(
      clock_i => reference_clock_i,
      data_i => reset_n_i,
      data_o => reference_reset_n_s
      );

  measurers: for i in 0 to measured_count_c-1 generate
    measurer: nsl_clocking.interdomain.clock_rate_measurer
      generic map(
        clock_i_hz_c => reference_hz_c,
        update_hz_l2_c => update_hz_l2_c
        )
      port map(
        clock_i => reference_clock_i,
        reset_n_i => reference_reset_n_s,
        measured_clock_i => measured_clock_i(i),
        rate_hz_o => rate_s(i)
        );

    rate_flat_s((i+1)*rate_width_c-1 downto i*rate_width_c)
      <= std_ulogic_vector(rate_s(i));
  end generate;

  -- The one crossing of the block, taken over the whole rate set: every
  -- measurer counts the same window off the same reference clock, so the
  -- rates change together on a single reference cycle and cross as one
  -- coherent snapshot.
  --
  -- The values are quasi-static -- constant for a whole update period, which
  -- is orders of magnitude longer than the few host cycles this takes -- so
  -- resynchronising them bit by bit is safe under a stability filter:
  -- interdomain_reg double-registers each bit in the host domain and forwards
  -- the word only once it has read back identical for stable_count_c
  -- consecutive host cycles. The one host cycle in which bits of a new rate
  -- may land out of step is therefore never the cycle that is published, and
  -- the register file only ever sees a value that stood still.
  rate_cdc: nsl_clocking.interdomain.interdomain_reg
    generic map(
      stable_count_c => 3,
      cycle_count_c => 2,
      data_width_c => measured_count_c*rate_width_c
      )
    port map(
      clock_i => clock_i,
      data_i => rate_flat_s,
      data_o => rate_host_s
      );

  regmap: nsl_amba.apb.apb_regmap
    generic map(
      config_c => apb_config_c,
      reg_count_l2_c => reg_count_l2_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,

      apb_i => apb_i,
      apb_o => apb_o,

      reg_no_o => reg_no_s,
      w_value_o => open,
      w_strobe_o => open,
      r_value_i => r_value_s,
      r_strobe_o => open
      );

  read_mux: process(reg_no_s, rate_host_s)
    variable value : unsigned(word_bits_c-1 downto 0);
  begin
    value := (others => '0');

    -- STATUS itself is reserved: a free-running measurement has no state to
    -- report. FINGERPRINT is what the host's status poll comes for.
    if reg_no_s = REG_FINGERPRINT_C then
      value := resize(fingerprint_c, word_bits_c);
    end if;

    for i in 0 to measured_count_c-1 loop
      if reg_no_s = REG_RATE_C + i then
        value := resize(
          unsigned(rate_host_s((i+1)*rate_width_c-1 downto i*rate_width_c)),
          word_bits_c);
      end if;
    end loop;

    r_value_s <= value;
  end process;

end architecture;
