library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- Measures where the rising edges of a few clocks sit relative to the
-- rising edge of the first one, counted in cycles of the measuring clock.
--
-- Every clock here comes from one PLL, so they are phase-coherent and
-- sampling one with another is a deterministic thing to do rather than a
-- race.  They reach a flip-flop's data input through general routing, which
-- is what makes this a measurement of the block's phase shift rather than
-- of a clock tree.
--
-- offset_o holds one count per clock, the first one's being meaningless.
-- settled_o states every clock has been seen rising since reset, so the
-- counts stand for something.
entity phase_meter is
  generic(
    clock_count_c : natural;
    -- Enough to hold one period of the measured clocks, counted in
    -- cycles of the measuring one.
    count_width_c : natural
    );
  port(
    clock_i : in std_ulogic_vector(0 to clock_count_c-1);

    meter_clock_i : in std_ulogic;
    meter_reset_n_i : in std_ulogic;

    offset_o : out unsigned(clock_count_c * count_width_c - 1 downto 0);
    settled_o : out std_ulogic
    );
end entity;

architecture beh of phase_meter is

  subtype count_t is unsigned(count_width_c-1 downto 0);
  type count_vector is array(natural range <>) of count_t;

  type regs_t is
  record
    -- Two stages, so an edge is told from a level whatever the routing
    -- delay does to the sampling point.
    sample, last : std_ulogic_vector(0 to clock_count_c-1);
    counter : count_t;
    offset : count_vector(0 to clock_count_c-1);
    seen : std_ulogic_vector(0 to clock_count_c-1);
  end record;

  signal r, rin : regs_t;

begin

  regs: process(meter_clock_i, meter_reset_n_i) is
  begin
    if rising_edge(meter_clock_i) then
      r <= rin;
    end if;

    if meter_reset_n_i = '0' then
      r.sample <= (others => '0');
      r.last <= (others => '0');
      r.counter <= (others => '0');
      r.offset <= (others => (others => '0'));
      r.seen <= (others => '0');
    end if;
  end process;

  transition: process(r, clock_i) is
    variable rising_v : std_ulogic_vector(0 to clock_count_c-1);
  begin
    rin <= r;

    rin.sample <= clock_i;
    rin.last <= r.sample;

    for i in 0 to clock_count_c-1 loop
      rising_v(i) := r.sample(i) and not r.last(i);
    end loop;

    -- The counter restarts on the reference clock's edge, and every other
    -- clock latches it as it rises: that count is the delay between them.
    -- It restarts at one rather than zero because the restart only takes
    -- effect on the next cycle, which is already one cycle of delay.
    if rising_v(0) = '1' then
      rin.counter <= to_unsigned(1, count_width_c);
    elsif r.counter /= (r.counter'range => '1') then
      rin.counter <= r.counter + 1;
    end if;

    for i in 0 to clock_count_c-1 loop
      if rising_v(i) = '1' then
        if rising_v(0) = '1' then
          -- Same cycle as the reference: no delay at all
          rin.offset(i) <= (others => '0');
        else
          rin.offset(i) <= r.counter;
        end if;
        rin.seen(i) <= '1';
      end if;
    end loop;
  end process;

  moore: process(r) is
  begin
    for i in 0 to clock_count_c-1 loop
      offset_o((i+1)*count_width_c-1 downto i*count_width_c) <= r.offset(i);
    end loop;

    settled_o <= '1';
    for i in 0 to clock_count_c-1 loop
      if r.seen(i) = '0' then
        settled_o <= '0';
      end if;
    end loop;
  end process;

end architecture;
