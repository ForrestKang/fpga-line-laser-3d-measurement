# New portable wrapper; original RTL/XDC/BD export are unchanged.
set root [file normalize [file join [file dirname [info script]] ..]]
set handoff [file join $root vendor r31_baseline_copy]
if {![file isdirectory $handoff]} {error "Missing private R43 vendor dependency: $handoff"}
if {[version -short] ne "2025.2"} {error "Use Vivado 2025.2; do not silently upgrade IP"}
if {![info exists build_dir]} {
 if {[llength $argv]<1} {error "tclargs: NEW_OUTPUT_DIRECTORY"}
 set build_dir [file normalize [lindex $argv 0]]
}
if {[file exists $build_dir]} {error "Output exists; choose a new directory: $build_dir"}
file mkdir $build_dir
set tree [file join $handoff source_tree]
create_project sl_profile_hdmi $build_dir -part xc7z020clg400-1
set_property XPM_LIBRARIES {XPM_CDC XPM_FIFO XPM_MEMORY} [current_project]
if {![info exists row_lanes] || $row_lanes ni {2 4 8}} {error "R43 row_lanes must explicitly be 2/4/8"}
if {![info exists row_dispatch] || $row_dispatch ni {0 1}} {error "R43 row_dispatch must explicitly be 0/1"}
if {![info exists banked_requests] || $banked_requests ni {0 1}} {error "R43 banked_requests must explicitly be 0/1"}
if {![info exists banked_results] || $banked_results ni {0 1}} {error "R43 banked_results must explicitly be 0/1"}
if {![info exists balanced_dispatch] || $balanced_dispatch ni {0 1}} {error "R43 balanced_dispatch must explicitly be 0/1"}
set_property verilog_define [list R43_ROW_LANES=$row_lanes R43_ROW_DISPATCH=$row_dispatch R43_BANKED_REQUESTS=$banked_requests R43_BANKED_RESULTS=$banked_results R43_BALANCED_DISPATCH=$balanced_dispatch] [get_filesets sources_1]
set_property ip_repo_paths [list [file join $handoff ip_repo]] [current_project]
update_ip_catalog
source [file join $handoff scripts project_inputs.tcl]
# R28 keeps the AXI/BD wrapper module ABI while explicitly binding the new frontend.
set production [file join $handoff r28_production]
set r28_rtl {}
foreach f $rtl_files {
 set base [file tail $f]
 switch $base {
  structured_light_motion_profile_hdmi_top.sv {lappend r28_rtl [file join $root rtl r42_board_top.sv]}
  pfo3_frontend_r25.sv {lappend r28_rtl [file join $root rtl r35_frontend.sv]}
  pfo3_row_r25.sv {lappend r28_rtl [file join $root rtl r35_row.sv]}
  pfo3_axi_r25.sv {lappend r28_rtl [file join $root rtl r35_axi.sv]}
  pfo3_axi_bd_r25.v {lappend r28_rtl [file join $root rtl pfo3_axi_bd_r25.v]}
  p2_capture_core.sv - p2_capture_metadata.sv - p2_processing_ip.sv - camera_signal_monitor.sv - pclk_frequency_meter.sv {lappend r28_rtl [file join $root rtl $base]}
  default {lappend r28_rtl $f}
 }
}
set rtl_files $r28_rtl
lappend rtl_files [file join $root baseline_rtl r33_direction.sv] [file join $root rtl r41_window_filter.sv]
foreach f {r35_geometry.sv r35_geo_mul64.sv r35_div_q30.sv r35_point_cache.sv} {lappend rtl_files [file join $root rtl $f]}
lappend rtl_files [file join $root rtl r35_direction.sv]
# R40 PL cloud view (instantiated inside pfo3_axi_r25).
foreach f {r40_cloud_view.sv r40_rams.sv r40_tables.sv r41_laser_ae.sv r41_measure.sv section_measure.sv} {lappend rtl_files [file join $root rtl $f]}
lappend rtl_files [file join $root rtl r42_buttons.sv]
add_files -norecurse $rtl_files
set_property include_dirs [file join $tree pl r25_next rtl] [get_filesets sources_1]
lappend xdc_files [file join $root constraints r42_buttons.xdc]
add_files -fileset constrs_1 -norecurse $xdc_files
set_property top structured_light_motion_profile_hdmi_top [get_filesets sources_1]
update_compile_order -fileset sources_1
# V9 block design plus the three asserted R40 edits (scripts/make_bd_r40.py).
source [file join $root bd block_design_r40.tcl]
set bd [get_files -quiet */sl_profile_hdmi.bd]
if {[llength $bd]!=1} {error "BD reconstruction failed"}
validate_bd_design
save_bd_design
set_property synth_checkpoint_mode None $bd
generate_target all $bd
update_compile_order -fileset sources_1
report_ip_status -file [file join $build_dir ip_status.rpt]
set fh [open [file join $build_dir compiled_sources.txt] w]
foreach s [get_files -compile_order sources -used_in synthesis] {puts $fh $s}
close $fh
puts R40_ISOLATED_PROJECT_CREATED
