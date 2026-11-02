# R43 post-route physical optimization; identical source netlist and constraints.
if {[llength $argv]!=2} {error "args NEW_OUTPUT STRATEGY"}
set build_dir [file normalize [lindex $argv 0]]
set strategy [lindex $argv 1]
if {[file exists $build_dir]} {error "Output exists"}
set r40 [file normalize [file join [file dirname [info script]] ..]]
set out [file join $build_dir reports]
set deliver [file join $build_dir artifacts]
file mkdir $out $deliver
set row_lanes 8
set_param general.maxThreads 2
if {$strategy eq "margin_alt"} {open_checkpoint D:/Workspace/CodexWork/scan3d_r43_timing_board_20261029_02/build/p02_margin/artifacts/route.dcp} else {open_checkpoint D:/Workspace/CodexWork/scan3d_r43_pl_balance_20261029_01/build/b03_8static_banked/artifacts/route.dcp}
report_timing_summary -delay_type min_max -file $out/before_timing.rpt
if {$strategy eq "margin"} {
 # Baseline effective XDC has no user uncertainty; optimize against a stricter temporary target.
 set_clock_uncertainty -setup 0.200 [get_clocks clk_fpga_1]
 report_timing_summary -file $out/temporary_stricter_target.rpt
 phys_opt_design -directive AggressiveExplore
 # Restore baseline user uncertainty (0); final reports use original clock periods/jitter.
 set_clock_uncertainty -setup 0.000 [get_clocks clk_fpga_1]
} elseif {$strategy eq "margin_alt"} {
 set_clock_uncertainty -setup 0.300 [get_clocks clk_fpga_1]
 phys_opt_design -directive AlternateReplication
 set_clock_uncertainty -setup 0.000 [get_clocks clk_fpga_1]
} elseif {$strategy eq "alternate"} {
 phys_opt_design -directive AlternateReplication
} elseif {$strategy eq "explore"} {
 phys_opt_design -directive Explore
} elseif {$strategy eq "rewire"} {
 phys_opt_design -rewire -critical_cell_opt -placement_opt -routing_opt
} else {error "Unknown strategy"}
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
if {[catch {exec {*}$gate_python -B [file join $r40 scripts review_build_r43.py] --reports $out --output [file join $build_dir ADMISSION_R43.json] --expected-lanes $row_lanes} gate_result]} {
    puts $gate_result
    error "R43 post-route gate rejected build. Reports preserved; no bit generated."
}
puts $gate_result
write_bitstream -bin_file $deliver/r43.bit
puts "R40_BUILD_COMPLETE WNS=$wns WHS=$whs $deliver"

