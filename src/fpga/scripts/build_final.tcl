# R40 full build: R39's passing flow (R37 final flow) with the PL cloud view added.
#   synth -> R37 private CDC + HDMI clock constraints -> opt Explore -> place ExtraTimingOpt
#   -> phys_opt AggressiveExplore -> route Explore -> post-route phys_opt AggressiveExplore
#   -> reports -> bitstream. New in R40: clk_fpga_0 <-> clk_fpga_1 crossings exist only
#   inside xpm_cdc macros and the dual-clock colour BRAM; the reports below prove it.
if {[llength $argv] != 6} {error "tclargs: NEW_OUTPUT_DIRECTORY ROW_LANES ROW_DISPATCH BANKED_REQUESTS BANKED_RESULTS BALANCED_DISPATCH"}
set build_dir [file normalize [lindex $argv 0]]
set row_lanes [lindex $argv 1]
set row_dispatch [lindex $argv 2]
set banked_requests [lindex $argv 3]
set banked_results [lindex $argv 4]
set balanced_dispatch [lindex $argv 5]
set r40 [file normalize [file join [file dirname [info script]] ..]]
source [file join $r40 scripts create_project_r43.tcl]
set out [file join $build_dir reports]
set deliver [file join $build_dir artifacts]
file mkdir $out $deliver
set_param general.maxThreads 2
set fh [open $out/final_compiled_sources.txt w]
foreach s [get_files -compile_order sources -used_in synthesis] {puts $fh $s}
close $fh
set source_python [auto_execok python]
if {$source_python eq ""} {error "Python required to hash actual synthesis sources"}
puts [exec {*}$source_python -B [file join $r40 scripts freeze_compiled_sources.py] --list $out/final_compiled_sources.txt --output $out/final_compiled_sources_sha256.json]
set_property synth_checkpoint_mode None [get_files */sl_profile_hdmi.bd]
synth_design -top structured_light_motion_profile_hdmi_top -part xc7z020clg400-1 -flatten_hierarchy rebuilt
write_checkpoint $deliver/synth.dcp
report_utilization -hierarchical -file $out/utilization_synth.rpt
set p2_out $out
set ::p2_mode structured_light_profile_hdmi
set_msg_config -id {Constraints 18-515} -new_severity ERROR
source [file join $r40 constraints constrain_cdc_r40_b09.tcl]
source [file join $handoff source_tree pl baseline scripts constrain_hdmi_clocks.tcl]
write_checkpoint $deliver/constrained_synth.dcp
opt_design -directive Explore
write_checkpoint $deliver/opt.dcp
place_design -directive ExtraTimingOpt
phys_opt_design -directive AggressiveExplore
route_design -directive Explore
phys_opt_design -directive AggressiveExplore
write_checkpoint $deliver/route.dcp
report_timing_summary -delay_type min_max -report_unconstrained -file $out/timing_route.rpt
report_utilization -hierarchical -file $out/utilization_route.rpt
report_methodology -file $out/methodology.rpt
report_drc -file $out/drc.rpt
report_route_status -file $out/route_status.rpt
report_cdc -details -file $out/cdc.rpt
report_cdc -from [get_clocks clk_fpga_0] -to [get_clocks clk_fpga_1] -details -file $out/cdc_view_a2v.rpt
report_cdc -from [get_clocks clk_fpga_1] -to [get_clocks clk_fpga_0] -details -file $out/cdc_view_v2a.rpt
report_clock_interaction -delay_type min_max -file $out/clock_interaction.rpt
report_bus_skew -warn_on_violation -file $out/bus_skew.rpt
check_timing -verbose -file $out/check_timing.rpt
report_timing -delay_type max -max_paths 30 -nworst 1 -unique_pins -sort_by slack -file $out/worst30.rpt
set view_cells [get_cells -hier -quiet -filter {NAME =~ *pfo3_frontend*/view/* && IS_PRIMITIVE}]
puts "R40_VIEW_PRIMITIVES [llength $view_cells]"
if {[llength $view_cells]} {catch {report_timing -delay_type max -max_paths 20 -nworst 1 -unique_pins -sort_by slack -through $view_cells -file $out/view_worst20.rpt}}
report_power -file $out/power_estimate.rpt
set pulse_report [report_pulse_width -all_violators -return_string]
set f [open $out/pulse_width.rpt w];puts $f $pulse_report;close $f
write_xdc -force $out/effective_constraints.xdc
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
set f [open $out/R40_TIMING.txt w];puts $f "WNS=$wns WHS=$whs";close $f
if {$wns <= 0 || $whs <= 0} {
    puts "R40_TIMING_FAILED_NO_BIT WNS=$wns WHS=$whs"
    exit 2
}
set gate_python [auto_execok python]
if {$gate_python eq ""} {error "Python required for fail-closed post-route R43 gate"}
if {[catch {exec {*}$gate_python -B [file join $r40 scripts review_build_final.py] --reports $out --output [file join $build_dir ADMISSION_FINAL.json]} gate_result]} {
    puts $gate_result
    error "R43 post-route gate rejected build. Reports preserved; no bit generated."
}
puts $gate_result
write_bitstream -bin_file $deliver/final.bit
puts "R40_BUILD_COMPLETE WNS=$wns WHS=$whs $deliver"
