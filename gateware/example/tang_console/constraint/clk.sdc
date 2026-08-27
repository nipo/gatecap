create_clock -name clk_50 -period 20 -waveform {0 10} [get_ports {clk_i}]
create_clock -name clk_50_buf -period 20 -waveform {0 10} [get_nets {clock_ext_s}]
