# Path-portable build; original arithmetic, pin/clock constraints and gates retained.
if {[llength $argv]!=2} {error "tclargs: NEW_OUTPUT_DIRECTORY PYTHON_EXECUTABLE"}
set build_dir [file normalize [lindex $argv 0]]
set py [file normalize [lindex $argv 1]]
if {![file isfile $py]} {error "Python executable missing"}
source [file join [file dirname [info script]] create_project_r28.tcl]
set out [file join $build_dir reports]
set deliver [file join $build_dir artifacts]
file mkdir $out $deliver
set_param general.maxThreads 2
set fh [open $out/final_compiled_sources.txt w]
foreach s [get_files -compile_order sources -used_in synthesis] {puts $fh $s}
close $fh
exec $py -I -B [file join $handoff scripts snapshot_compiled.py] $out/source_sha256.json --build-dir $build_dir --source-list $out/final_compiled_sources.txt
set_property synth_checkpoint_mode None [get_files */sl_profile_hdmi.bd]
synth_design -top structured_light_motion_profile_hdmi_top -part xc7z020clg400-1 -flatten_hierarchy rebuilt
write_checkpoint $deliver/synth.dcp
report_utilization -hierarchical -file $out/utilization_synth.rpt
set p2_out $out
set ::p2_mode structured_light_profile_hdmi
source $tree/pl/baseline/scripts/constrain_cdc.tcl
source $tree/pl/baseline/scripts/constrain_hdmi_clocks.tcl
opt_design
place_design
phys_opt_design
route_design
write_checkpoint $deliver/route.dcp
report_timing_summary -delay_type min_max -report_unconstrained -file $out/timing_route.rpt
report_utilization -hierarchical -file $out/utilization_route.rpt
report_methodology -file $out/methodology.rpt
report_drc -file $out/drc.rpt
report_route_status -file $out/route_status.rpt
report_cdc -details -file $out/cdc.rpt
set pulse_report [report_pulse_width -all_violators -return_string]
set f [open $out/pulse_width.rpt w];puts $f $pulse_report;close $f
write_xdc -force $out/effective_constraints.xdc
if {[get_property SLACK [get_timing_paths -delay_type max -max_paths 1]] < 0} {error R28_SETUP_FAIL_NO_BIT}
if {[get_property SLACK [get_timing_paths -delay_type min -max_paths 1]] < 0} {error R28_HOLD_FAIL_NO_BIT}
if {[regexp {VIOLATED|-[0-9]+\.[0-9]+} $pulse_report]} {error R28_PULSE_WIDTH_FAIL_NO_BIT}
foreach violation [get_drc_violations] {
    if {[string equal -nocase [get_property SEVERITY $violation] Error]} {error R28_DRC_ERROR_NO_BIT}
}
set lut -1
set utilization [report_utilization -hierarchical -return_string]
foreach line [split $utilization \n] {
    if {[string match {| structured_light_motion_profile_hdmi_top *} $line]} {
        set lut [string trim [lindex [split $line |] 3]]
        break
    }
}
if {![string is integer -strict $lut] || $lut<0} {error R28_LUT_TOTAL_UNAVAILABLE_NO_BIT}
set ff [llength [get_cells -hier -filter {REF_NAME =~ FD*}]]
set b36 [llength [get_cells -hier -filter {REF_NAME =~ RAMB36*}]]
set b18 [llength [get_cells -hier -filter {REF_NAME =~ RAMB18*}]]
set dsp [llength [get_cells -hier -filter {REF_NAME =~ DSP48*}]]
if {$lut>37240 || $ff>58520 || ($b36*2+$b18)>196 || $dsp>110} {error R28_RESOURCE_BUDGET_FAIL_NO_BIT}
set f [open $out/hardware_counts.txt w]
puts $f "LUT=$lut FF=$ff RAMB36=$b36 RAMB18=$b18 DSP=$dsp"
close $f
exec $py -I -B [file join $handoff scripts snapshot_compiled.py] $out/source_sha256.json --build-dir $build_dir --source-list $out/final_compiled_sources.txt --verify
# Correctness gate is checked immediately before producing deployable artifacts.
set gate [file normalize [file join $handoff .. .. work pl full480 PASS.json]]
if {![file isfile $gate]} {error R28_FULL480_CORRECTNESS_GATE_PENDING_NO_BIT}
set gate_f [open $gate r];set gate_text [read $gate_f];close $gate_f
if {![regexp {"frames": 480} $gate_text] || ![regexp {"variant_runs": 1920} $gate_text]} {error R28_FULL480_CORRECTNESS_GATE_INVALID_NO_BIT}
write_bitstream $deliver/r28.bit
set hwh [glob $build_dir/sl_profile_hdmi.gen/sources_1/bd/sl_profile_hdmi/hw_handoff/*.hwh]
file copy [lindex $hwh 0] $deliver/r28.hwh
puts "R28_FULL_BUILD_BIT_COMPLETE $deliver reports=$out"

