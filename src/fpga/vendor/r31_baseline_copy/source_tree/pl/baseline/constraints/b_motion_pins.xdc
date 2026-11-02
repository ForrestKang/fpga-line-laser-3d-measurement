# Stage-B direct-wire motor permit outputs for the structured-light candidate.
# The direct artifact maps motor_enable to active-low EN.  An expansion build
# may map the same package pin to enable_request; the carrier mode is a
# read-only build parameter and is not changed at runtime.
set_property PACKAGE_PIN W14 [get_ports motor_step]
set_property PACKAGE_PIN Y14 [get_ports motor_dir]
set_property PACKAGE_PIN T11 [get_ports motor_enable]
set_property PACKAGE_PIN T10 [get_ports motor_heartbeat]
set_property IOSTANDARD LVCMOS33 [get_ports {motor_step motor_dir motor_enable motor_heartbeat}]
set_property DRIVE 8 [get_ports {motor_step motor_dir motor_enable motor_heartbeat}]
set_property SLEW SLOW [get_ports {motor_step motor_dir motor_enable motor_heartbeat}]
