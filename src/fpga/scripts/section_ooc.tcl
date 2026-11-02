# Out-of-context implementation of section_measure alone at 100 MHz: resources and timing.
set out [file normalize [lindex $argv 0]]
if {[file exists $out]} { error "Refusing to overwrite existing build: $out" }
file mkdir $out
set_param general.maxThreads 2
read_verilog -sv [file normalize [file join [file dirname [info script]] .. rtl section_measure.sv]]
synth_design -top section_measure -part xc7z020clg400-1 -mode out_of_context -flatten_hierarchy rebuilt
create_clock -period 10.000 -name clk [get_ports clk]
opt_design
write_checkpoint $out/optimized.dcp
place_design -directive ExtraTimingOpt
report_timing -max_paths 10 -nworst 1 -unique_pins -file $out/placed_worst10.rpt
write_checkpoint $out/placed.dcp
phys_opt_design
route_design
write_checkpoint $out/routed.dcp
report_utilization -file $out/utilization.rpt
report_utilization -hierarchical -file $out/utilization_hier.rpt
report_timing_summary -file $out/timing.rpt
report_timing -max_paths 10 -nworst 1 -unique_pins -file $out/worst10.rpt
set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -hold]]
puts "SECTION_OOC WNS=$wns WHS=$whs"
set cells [get_cells -hierarchical -filter {REF_NAME =~ DSP48*}]
set fd [open $out/summary.json w]
puts $fd "{\"wns_ns\": $wns, \"whs_ns\": $whs, \"dsp48_cells\": [llength $cells]}"
close $fd
