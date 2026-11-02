# Apply ONLY reviewed first-stage, stable-mailbox and asynchronous-assert paths.
# No asynchronous clock groups and no whole-module false paths.
set cdc_fh [open [file join $p2_out cdc_constraint_inventory.txt] w]
proc p2_cdc_dest {expression label} {
    global cdc_fh
    set cells [get_cells -quiet -hier -regexp $expression]
    set pins [get_pins -quiet -of_objects $cells -filter {REF_PIN_NAME == D}]
    puts $cdc_fh "$label count=[llength $pins]"
    foreach p $pins {puts $cdc_fh "  $p"}
    return $pins
}
proc p2_cdc_sources {expression label} {
    global cdc_fh
    set cells [get_cells -quiet -hier -regexp $expression]
    set pins [get_pins -quiet -of_objects $cells -filter {REF_PIN_NAME == Q}]
    puts $cdc_fh "$label count=[llength $pins]"
    foreach p $pins {puts $cdc_fh "  $p"}
    return $pins
}
set sys [get_clocks clk_fpga_0]
set cam [get_clocks cam_pclk]
set to_sys [p2_cdc_dest {.*u_core/(cam_fault(_flags)?_sync1|cam_frame_count_sync1|geometry_toggle_sync1|raw_wr_busy_sync1|row_wr_busy_sync1|cam_ready_sync1)_reg(\[[0-9]+\])?} camera_to_system_first_stage]
if {[llength $to_sys]} {set_max_delay -datapath_only 10.0 -from $cam -to $to_sys}
set meta_toggle [p2_cdc_dest {.*u_metadata/toggle_sync1_reg} metadata_sof_toggle_first_stage]
set meta_data [p2_cdc_dest {.*u_metadata/(id|epoch)_sync1_reg(\[[0-9]+\])?} metadata_sof_identity_mailbox]
if {![llength $meta_toggle] || [llength $meta_data] != 48} {
    error "SOF metadata mailbox missing or width mismatch"
}
set_max_delay -datapath_only 10.0 -from $cam -to $meta_toggle
set_max_delay -datapath_only 10.0 -from $cam -to $meta_data
set req [p2_cdc_dest {.*u_core/req_sync1_reg} request_toggle_first_stage]
if {![llength $req]} {error "Request toggle synchronizer missing from synthesized design"}
set_max_delay -datapath_only 10.0 -from $sys -to $req
set gray [p2_cdc_dest {.*u_pclk_meter/gray_sync_1_reg\[[0-9]+\]} pclk_gray_first_stage]
if {![llength $gray]} {error "PCLK Gray synchronizer missing"}
set_max_delay -datapath_only 10.0 -from $cam -to $gray
set gray_sources [get_pins -quiet -of_objects [get_cells -quiet -hier -regexp {.*u_pclk_meter/cam_count_gray_reg\[[0-9]+\]}] -filter {REF_PIN_NAME == C}]
if {![llength $gray_sources]} {error "PCLK registered Gray source missing"}
set_bus_skew 10.0 -from $gray_sources -to $gray
set counts [p2_cdc_dest {.*u_core/(cam_frame_count_sync1)_reg\[[0-9]+\]} frame_gray_first_stage]
if {[llength $counts]} {
    set count_sources [get_pins -quiet -of_objects [get_cells -quiet -hier -regexp {.*u_core/cam_frame_count_gray_reg\[[0-9]+\]}] -filter {REF_PIN_NAME == C}]
    if {[llength $count_sources]} {set_bus_skew 10.0 -from $count_sources -to $counts}
}
# Source configuration is latched with request toggle and held for the entire
# request. Destination consumes it only after its two-stage request arrives.
set config [p2_cdc_dest {.*u_core/cam_(profile_id|epoch|threshold|peak|x0|x1|y0|y1|minw|maxw|sum|selected_mode)_reg(\[[0-9]+\])?} held_configuration_mailbox]
if {[llength $config]} {set_max_delay -datapath_only 10.0 -from $sys -to $config}
# EOF geometry is stable before the synchronized toggle commits its snapshot.
set geometry [p2_cdc_dest {.*u_core/geometry_(line|frame|eof)_sync1_reg(\[[0-9]+\])?} held_geometry_mailbox]
if {[llength $geometry]} {set_max_delay -datapath_only 10.0 -from $cam -to $geometry}
# Passive FV/LV monitor: command, acknowledgement and result toggles are
# synchronized as single bits. The associated payload is held in its source
# domain until the destination has captured it; constrain every payload bit
# directly and check that all expected pins exist. This is a bundled-data
# mailbox, not a bank of independent synchronizer flops.
set mon_cmd_sync [p2_cdc_dest {.*u_signal_monitor/cmd_sync1_reg} monitor_command_toggle_first_stage]
set mon_ack_sync [p2_cdc_dest {.*u_signal_monitor/cmd_ack_sync1_reg} monitor_ack_toggle_first_stage]
set mon_done_sync [p2_cdc_dest {.*u_signal_monitor/done_sync1_reg} monitor_done_toggle_first_stage]
foreach {pins clk label} [list $mon_cmd_sync $sys monitor_command $mon_ack_sync $cam monitor_ack $mon_done_sync $cam monitor_done] {
    if {![llength $pins]} {error "$label toggle synchronizer first stage missing"}
    set_max_delay -datapath_only 10.0 -from $clk -to $pins
}
set mon_cmd_cells [get_cells -quiet -hier -regexp {.*u_signal_monitor/cmd_(is_start|result_id)_reg(\[[0-9]+\])?}]
set mon_cmd_src [get_pins -quiet -of_objects $mon_cmd_cells -filter {REF_PIN_NAME == C}]
puts $cdc_fh "monitor_command_payload_launch count=[llength $mon_cmd_src]"
foreach p $mon_cmd_src {puts $cdc_fh "  $p"}
set mon_cmd_dst [p2_cdc_dest {.*u_signal_monitor/cmd_(start|result_id)_stage_cam_reg(\[[0-9]+\])?} monitor_command_payload_capture]
if {[llength $mon_cmd_src] != 33 || [llength $mon_cmd_dst] != 33} {
    error "Monitor command mailbox width mismatch: source=[llength $mon_cmd_src] destination=[llength $mon_cmd_dst]"
}
set_max_delay -datapath_only 10.0 -from $sys -to $mon_cmd_dst
set_bus_skew 10.0 -from $mon_cmd_src -to $mon_cmd_dst
set mon_result_cells [get_cells -quiet -hier -regexp {.*u_signal_monitor/done_(result_id|error_flags|fv_rises|fv_falls|lv_rises|lv_falls|fv_high_cycles|fv_low_cycles|lv_high_cycles|lv_low_cycles|line_min|line_max|rows_min|rows_max|frames|period_min|period_max|pixel_min|pixel_max|data_variation)_cam_reg(\[[0-9]+\])?}]
set mon_result_src [get_pins -quiet -of_objects $mon_result_cells -filter {REF_PIN_NAME == C}]
puts $cdc_fh "monitor_result_payload_launch count=[llength $mon_result_src]"
foreach p $mon_result_src {puts $cdc_fh "  $p"}
set mon_result_dst [p2_cdc_dest {.*u_signal_monitor/done_(result_id|error_flags|fv_rises|fv_falls|lv_rises|lv_falls|fv_high_cycles|fv_low_cycles|lv_high_cycles|lv_low_cycles|line_min|line_max|rows_min|rows_max|frames|period_min|period_max|pixel_min|pixel_max|data_variation)_stage_sys_reg(\[[0-9]+\])?} monitor_result_payload_capture]
if {[llength $mon_result_src] != [llength $mon_result_dst] || [llength $mon_result_src] < 500} {
    error "Monitor result mailbox width mismatch: source=[llength $mon_result_src] destination=[llength $mon_result_dst]"
}
set_max_delay -datapath_only 10.0 -from $cam -to $mon_result_dst
set_bus_skew 10.0 -from $mon_result_src -to $mon_result_dst
# The only asynchronous assertion exceptions are reset synchronizer PRE/CLR.
# Functional D/CE/R paths after the local release stage remain timed.
set reset_cells [get_cells -quiet -hier -regexp {.*(u_core/cam_reset_pipe_reg|u_metadata/cam_reset_pipe_reg|u_pclk_meter/cam_reset_pipe_reg|u_signal_monitor/cam_reset_sync[12]_reg|u_(raw|row)_fifo/(wr_release_reg|rd_release_reg|reset_hold_reg))(\[[0-9]+\])?}]
set reset_pins [get_pins -quiet -of_objects $reset_cells -filter {REF_PIN_NAME == PRE || REF_PIN_NAME == CLR}]
puts $cdc_fh "asynchronous_assertion_only count=[llength $reset_pins]"
foreach p $reset_pins {puts $cdc_fh "  $p"}
if {[llength $reset_pins]} {set_false_path -to $reset_pins}
# SCCB has no synchronous external clock. Only its first two-flop input
# synchronizer stage is excepted; all later stages and the FSM remain timed.
foreach {port expression} {
    cam_scl {.*(scl_sync1|scl_sync_1)_reg}
    cam_sda {.*(sda_sync1|sda_sync_1)_reg}
} {
    set pins [p2_cdc_dest $expression ${port}_external_first_stage]
    if {![llength $pins]} {error "$port two-flop input synchronizer missing"}
    set_false_path -from [get_ports $port] -to $pins
}
close $cdc_fh
file copy -force [file join $p2_out cdc_constraint_inventory.txt] [file join $p2_out cdc_endpoint_inventory.txt]
report_exceptions -file [file join $p2_out cdc_exceptions.rpt]
