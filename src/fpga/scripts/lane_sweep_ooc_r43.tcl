# R43 row-engine OOC synthesis only. No board, no modification of R42.
set root [file normalize [file join [file dirname [info script]] ..]]
if {[llength $argv] < 2 || [llength $argv] > 6} {error "usage: lane_sweep_ooc_r43.tcl OUT LANES ?DISPATCH? ?BANKED_REQUESTS? ?BANKED_RESULTS? ?BALANCED_DISPATCH?"}
set out [file normalize [lindex $argv 0]]
set lanes [lindex $argv 1]
set dispatch 0
if {[llength $argv] >= 3} {set dispatch [lindex $argv 2]}
set banked 0
if {[llength $argv] >= 4} {set banked [lindex $argv 3]}
set banked_results 0
if {[llength $argv] >= 5} {set banked_results [lindex $argv 4]}
set balanced 0
if {[llength $argv] == 6} {set balanced [lindex $argv 5]}
if {$balanced ni {0 1}} {error "BALANCED_DISPATCH must be 0/1"}
if {$banked_results ni {0 1}} {error "BANKED_RESULTS must be 0/1"}
if {$banked ni {0 1}} {error "BANKED_REQUESTS must be 0/1"}
if {$dispatch ni {0 1}} {error "DISPATCH must be 0/1"}
if {$lanes ni {2 4 8}} {error "LANES must be 2/4/8"}
file mkdir $out
file mkdir $out/sources
foreach name {shared_window_filter.sv direction_refiner.sv stripe_extractor.sv} {file copy $root/rtl/$name $out/sources/$name}
read_verilog -sv [list $out/sources/shared_window_filter.sv $out/sources/direction_refiner.sv $out/sources/stripe_extractor.sv]
synth_design -mode out_of_context -top stripe_extractor -part xc7z020clg400-1 -generic [list LANES=$lanes DISPATCH=$dispatch BANKED_REQUESTS=$banked BANKED_RESULTS=$banked_results BALANCED_DISPATCH=$balanced]
create_clock -name sys_clk -period 10.000 [get_ports clk]
report_utilization -hierarchical -file $out/utilization.rpt
report_timing_summary -file $out/timing_synth.rpt
write_checkpoint -force $out/synth.dcp
set fp [open $out/RESULT.txt w]
puts $fp "evidence_kind ROW_OOC_SYNTH_NOT_ROUTED_NOT_BOARD"
puts $fp "lanes $lanes"
puts $fp "dispatch $dispatch"
puts $fp "banked_requests $banked"
puts $fp "banked_results $banked_results"
puts $fp "balanced_dispatch $balanced"
foreach {label filter} {DSP {REF_NAME =~ DSP*} LUT {REF_NAME =~ LUT*} FF {REF_NAME =~ FD*} BRAM {REF_NAME =~ RAMB*}} {
 puts $fp "$label [llength [get_cells -hier -filter $filter]]"
}
close $fp
exit
