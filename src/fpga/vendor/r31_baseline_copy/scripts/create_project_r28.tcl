# New portable wrapper; original RTL/XDC/BD export are unchanged.
set handoff [file normalize [file join [file dirname [info script]] ..]]
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
set_property ip_repo_paths [list [file join $handoff ip_repo]] [current_project]
update_ip_catalog
source [file join $handoff scripts project_inputs.tcl]
# R28 keeps the AXI/BD wrapper module ABI while explicitly binding the new frontend.
set production [file join $handoff r28_production]
set r28_rtl {}
foreach f $rtl_files {
 set base [file tail $f]
 switch $base {
  pfo3_frontend_r25.sv {lappend r28_rtl [file join $production pfo3_frontend_r28.sv]}
  pfo3_row_r25.sv {lappend r28_rtl [file join $production pfo3_row_r28.sv]}
  pfo3_axi_r25.sv {lappend r28_rtl [file join $production pfo3_axi_r25.sv]}
  default {lappend r28_rtl $f}
 }
}
set rtl_files $r28_rtl
add_files -norecurse $rtl_files
set_property include_dirs [file join $tree pl r25_next rtl] [get_filesets sources_1]
add_files -fileset constrs_1 -norecurse $xdc_files
set_property top structured_light_motion_profile_hdmi_top [get_filesets sources_1]
update_compile_order -fileset sources_1
source [file join $handoff bd block_design_v9.original.tcl]
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
puts R28_PORTABLE_PROJECT_CREATED
