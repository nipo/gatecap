create_clock -name clk_50 -period 20 -waveform {0 10} [get_ports {clk_i}]
#create_clock -name clk_50_buf -period 20 -waveform {0 10} [get_nets {clock_ext_s}]
#create_clock -name clk_usb_60 -period 16.666 -waveform {0 8.333} [get_nets {clock_usb_s}]
