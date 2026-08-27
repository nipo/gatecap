library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_synthesis, gatecap;
use nsl_amba.apb.all;
use gatecap.bus_explorer.all;

-- Target-domain half of the bus-explorer instrument: an APB master that
-- executes one crossed command and answers with one crossed response.
--
-- It holds no state beyond the transaction in flight and no timeout of its
-- own. An access against a target that never asserts pready stays in its
-- access phase for as long as the target takes; the shell abandons it,
-- flips its tag, and drops the answer when it finally arrives. Timing out
-- here instead would mean driving an APB transfer to an end the target
-- never agreed to, which the protocol has no room for.
--
-- It contains no clock crossing: the assembler wires it to the shell (see
-- the bus_explorer package for the contract).
entity bus_explorer_core is
  generic (
    target_address_width_c : natural range 1 to 32 := 32;
    target_data_width_c : natural range 1 to 32 := 32
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    -- From the shell.
    command_i : in std_ulogic_vector(
      command_width(target_address_width_c, target_data_width_c)-1 downto 0);
    command_valid_i : in std_ulogic;
    command_ready_o : out std_ulogic;

    -- To the shell.
    response_o : out std_ulogic_vector(
      response_width(target_data_width_c)-1 downto 0);
    response_valid_o : out std_ulogic;
    response_ready_i : in std_ulogic;

    -- Target bus, dimensioned by target_apb_config of the generics above.
    apb_o : out master_t;
    apb_i : in slave_t
    );
end entity;

architecture rtl of bus_explorer_core is

  constant target_config_c : config_t :=
    target_apb_config(target_address_width_c, target_data_width_c);
  constant bus_bits_c : natural := 8 * 2**target_config_c.data_bus_width_l2;

  constant widths_legal_c : boolean :=
    target_address_width_c >= 1 and target_address_width_c <= 32
    and target_data_width_c >= 1 and target_data_width_c <= 32;

  type state_t is (
    ST_RESET,
    ST_IDLE,
    ST_SETUP,
    ST_ACCESS,
    ST_RESPOND
    );

  type regs_t is
  record
    state : state_t;
    write : std_ulogic;
    tag : std_ulogic;
    error : std_ulogic;
    address : unsigned(target_address_width_c-1 downto 0);
    wdata : unsigned(target_data_width_c-1 downto 0);
    rdata : unsigned(target_data_width_c-1 downto 0);
  end record;

  signal r, rin : regs_t;

begin

  assert widths_legal_c
    report "bus explorer target widths must be in 1 to 32"
    severity failure;

  widths_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "bus explorer target widths must be in 1 to 32",
      condition_c => widths_legal_c
      )
    port map(
      unused_i => '0'
      );

  regs: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      r <= rin;
    end if;
    if reset_n_i = '0' then
      r.state <= ST_RESET;
    end if;
  end process;

  transition: process(r, command_i, command_valid_i, response_ready_i, apb_i)
  begin
    rin <= r;

    case r.state is
      when ST_RESET =>
        rin.state <= ST_IDLE;

      when ST_IDLE =>
        if command_valid_i = '1' then
          rin.write <= command_write(command_i);
          rin.tag <= command_tag(command_i);
          rin.address <= command_address(command_i, target_address_width_c);
          rin.wdata <= command_wdata(command_i, target_address_width_c,
                                     target_data_width_c);
          rin.rdata <= (others => '0');
          rin.error <= '0';
          rin.state <= ST_SETUP;
        end if;

      when ST_SETUP =>
        rin.state <= ST_ACCESS;

      when ST_ACCESS =>
        if is_ready(target_config_c, apb_i) then
          if is_error(target_config_c, apb_i) then
            rin.error <= '1';
          end if;
          if r.write = '0' then
            rin.rdata <= resize(value(target_config_c, apb_i),
                                target_data_width_c);
          end if;
          rin.state <= ST_RESPOND;
        end if;

      when ST_RESPOND =>
        if response_ready_i = '1' then
          rin.state <= ST_IDLE;
        end if;
    end case;
  end process;

  mealy: process(r)
    variable phase_v : phase_t;
  begin
    command_ready_o <= '0';
    response_valid_o <= '0';
    apb_o <= transfer_idle(target_config_c);

    case r.state is
      when ST_IDLE =>
        command_ready_o <= '1';

      when ST_SETUP | ST_ACCESS =>
        if r.state = ST_SETUP then
          phase_v := PHASE_SETUP;
        else
          phase_v := PHASE_ACCESS;
        end if;

        -- Bits of the bus above the target data width are driven low.
        if r.write = '1' then
          apb_o <= write_transfer(target_config_c,
                                  addr => r.address,
                                  value => resize(r.wdata, bus_bits_c),
                                  phase => phase_v);
        else
          apb_o <= read_transfer(target_config_c,
                                 addr => r.address,
                                 phase => phase_v);
        end if;

      when ST_RESPOND =>
        response_valid_o <= '1';

      when others =>
        null;
    end case;
  end process;

  response_o <= response_pack(target_data_width_c,
                              error => r.error,
                              tag => r.tag,
                              rdata => r.rdata);

end architecture;
