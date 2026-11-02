# Read-only comparison of original and chosen optimized design constraints/clocks.
if {[llength $argv]!=2} {error "args CANDIDATE_DCP NEW_REPORT_DIRECTORY"}
set candidate [file normalize [lindex $argv 0]]
set output [file normalize [lindex $argv 1]]
if {[file exists $output]} {error "Output exists"}
file mkdir $output
foreach name {baseline candidate} {
 if {$name eq "baseline"} {set dcp D:/Workspace/CodexWork/scan3d_r43_pl_balance_20261029_01/build/b03_8static_banked/artifacts/route.dcp} else {set dcp $candidate}
 open_checkpoint $dcp
 report_clocks -file $output/${name}_clocks.rpt
 report_timing -from [get_clocks clk_fpga_1] -to [get_clocks clk_fpga_1] -delay_type max -max_paths 1 -file $output/${name}_clk1_timing.rpt
 write_xdc -force $output/${name}_constraints.xdc
 set f [open $output/${name}_clock_properties.txt w]
 foreach c [lsort [get_clocks]] {
  puts $f "CLOCK $c"
  foreach p [lsort [list_property $c]] {
   if {[regexp {PERIOD|WAVEFORM|UNCERTAINTY|SOURCE|MASTER|IS_GENERATED|MULTIPLY|DIVIDE|PHASE|JITTER} $p]} {puts $f "$p=[get_property $p $c]"}
  }
 }
 close $f
 set f [open $output/${name}_registers.txt w]
 foreach c [lsort [get_cells -hier -filter {REF_NAME =~ FD*}]] {puts $f "$c|[get_property REF_NAME $c]"}
 close $f
 close_design
}
puts R43_CLOCK_CONSTRAINT_COMPARISON_COMPLETE


