# Sensor timing: local MT9V034 DS Rev.G Table18/page52, falling PCLK data edge.
# Document tPD [-3,3]ns; LV/FV [5,9]ns. Add +/-1ns manual-wire skew budget.
# Actual mode, clock and board delays must be verified at Gate A.
create_clock -name cam_pclk -period 41.666667 [get_ports cam_pclk]
set_input_delay -clock cam_pclk -clock_fall -min -4.0 [get_ports {cam_data[*]}]
set_input_delay -clock cam_pclk -clock_fall -max 4.0 [get_ports {cam_data[*]}]
set_input_delay -clock cam_pclk -clock_fall -min 4.0 [get_ports {cam_fv cam_lv}]
set_input_delay -clock cam_pclk -clock_fall -max 10.0 [get_ports {cam_fv cam_lv}]
# Slow open-drain SCCB pins are independently synchronized at their input.
# Exact first-stage exceptions are installed post-synthesis, with path inventory.
set_input_delay -clock [get_clocks -quiet -include_generated_clocks clk_fpga_0] -max 10.0 [get_ports {cam_scl cam_sda}]
set_input_delay -clock [get_clocks -quiet -include_generated_clocks clk_fpga_0] -min 0.0 [get_ports {cam_scl cam_sda}]
# These are asynchronous/slow sensor control pins, not inputs sampled by
# FCLK0 at 100 MHz. MT9V034 Rev.G p60 specifies RESET_BAR >=750ns and a
# 10-SYSCLK wait before SCCB; Table19 specifies >=100ns data setup at 400kHz.
# Zero external clock-relative delay identifies the board boundary; the
# explicit 20ns propagation budget below bounds each control output path.
# This engineering budget does not relax internal 10ns SYS register paths.
set_output_delay -clock [get_clocks -quiet -include_generated_clocks clk_fpga_0] -max 0.0 [get_ports {cam_reset_n cam_standby cam_scl cam_sda}]
set_output_delay -clock [get_clocks -quiet -include_generated_clocks clk_fpga_0] -min 0.0 [get_ports {cam_reset_n cam_standby cam_scl cam_sda}]
set_max_delay -datapath_only 20.0 -from [get_clocks -quiet -include_generated_clocks clk_fpga_0] -to [get_ports {cam_reset_n cam_standby cam_scl cam_sda}]
