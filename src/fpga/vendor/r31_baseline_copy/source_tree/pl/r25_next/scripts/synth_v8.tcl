set root [file normalize [file join [file dirname [info script]] ..]]
set build [file join $root build synth_v8]
file mkdir $build
catch {close_project}
create_project r25_synth $build -part xc7z020clg400-1 -force
add_files [glob [file join $root rtl *.sv]]
set_property include_dirs [file join $root rtl] [get_filesets sources_1]
set_property top pfo3_frontend_r25 [get_filesets sources_1]
set xdc [open [file join $build clock.xdc] w]
puts $xdc {create_clock -period 10.000 [get_ports clk]}
close $xdc
add_files -fileset constrs_1 [file join $build clock.xdc]
update_compile_order -fileset sources_1
synth_design -top pfo3_frontend_r25 -part xc7z020clg400-1 -mode out_of_context
report_utilization -file [file join $build utilization.rpt]
report_timing_summary -file [file join $build timing_synth.rpt]
write_checkpoint -force [file join $build pfo3_frontend_r25_synth.dcp]
opt_design
place_design
route_design
report_utilization -file [file join $build utilization_route.rpt]
report_timing_summary -file [file join $build timing_route.rpt]
report_drc -file [file join $build drc_route.rpt]
write_checkpoint -force [file join $build pfo3_frontend_r25_route.dcp]

