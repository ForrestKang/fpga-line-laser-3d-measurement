# Run in the prepared, isolated R25 project; no bit on any timing/budget failure.
set root D:/Workspace/CodexWork/scan3d_pixel_frontend_20261021_25
set build_id [clock format [clock seconds] -format %Y%m%d_%H%M%S]
set out $root/evidence/full_r25/build_$build_id
set deliver $root/delivery/build_$build_id
set ::env(TEMP) D:/Workspace/CodexTemp/scan3d_r25
set ::env(TMP) $::env(TEMP)
file mkdir $out
file mkdir $deliver
set_param general.maxThreads 2
update_compile_order -fileset sources_1
set f [open $out/final_compiled_sources.txt w]
foreach s [get_files -compile_order sources -used_in synthesis] {puts $f $s}
close $f
set py D:/NUECSDC-FPGA/structured_light_system_algorithm_v1/.venv/Scripts/python.exe
exec $py -E -B $root/host/hash_compiled_r25.py $out/final_compiled_sources.txt $out/source_sha256.json
set_property synth_checkpoint_mode None [get_files */sl_profile_hdmi.bd]
synth_design -top structured_light_motion_profile_hdmi_top -part xc7z020clg400-1 -flatten_hierarchy rebuilt
write_checkpoint $deliver/synth.dcp
report_utilization -hierarchical -file $out/utilization_synth.rpt
set p2_out $out
set ::p2_mode structured_light_profile_hdmi
source $root/pl/baseline/scripts/constrain_cdc.tcl
source $root/pl/baseline/scripts/constrain_hdmi_clocks.tcl
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
if {[get_property SLACK [get_timing_paths -delay_type max -max_paths 1]] < 0} {error R25_SETUP_FAIL_NO_BIT}
if {[get_property SLACK [get_timing_paths -delay_type min -max_paths 1]] < 0} {error R25_HOLD_FAIL_NO_BIT}
if {[regexp {VIOLATED|-[0-9]+\.[0-9]+} $pulse_report]} {error R25_PULSE_WIDTH_FAIL_NO_BIT}
foreach violation [get_drc_violations] {
    if {[string equal -nocase [get_property SEVERITY $violation] Error]} {error R25_DRC_ERROR_NO_BIT}
}
set lut -1
set utilization [report_utilization -hierarchical -return_string]
foreach line [split $utilization \n] {
    if {[string match {| structured_light_motion_profile_hdmi_top *} $line]} {
        set lut [string trim [lindex [split $line |] 3]]
        break
    }
}
if {![string is integer -strict $lut] || $lut<0} {error R25_LUT_TOTAL_UNAVAILABLE_NO_BIT}
set ff [llength [get_cells -hier -filter {REF_NAME =~ FD*}]]
set b36 [llength [get_cells -hier -filter {REF_NAME =~ RAMB36*}]]
set b18 [llength [get_cells -hier -filter {REF_NAME =~ RAMB18*}]]
set dsp [llength [get_cells -hier -filter {REF_NAME =~ DSP48*}]]
if {$lut>37240 || $ff>58520 || ($b36*2+$b18)>196 || $dsp>110} {error R25_RESOURCE_BUDGET_FAIL_NO_BIT}
set f [open $out/hardware_counts.txt w]
puts $f "LUT=$lut FF=$ff RAMB36=$b36 RAMB18=$b18 DSP=$dsp"
close $f
exec $py -E -B $root/host/hash_compiled_r25.py $out/final_compiled_sources.txt $out/source_sha256.json --verify
write_bitstream $deliver/r25.bit
set hwh [glob $root/pl/build/full_project_r25/sl_profile_hdmi.gen/sources_1/bd/sl_profile_hdmi/hw_handoff/*.hwh]
file copy [lindex $hwh 0] $deliver/r25.hwh
puts "R25_FULL_BUILD_BIT_COMPLETE $deliver reports=$out"
