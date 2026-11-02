set root [file normalize [file join [file dirname [info script]] ..]]
set build [file join $root build sim]
file mkdir $build
create_project r25_sim $build -part xc7z020clg400-1 -force
add_files [glob [file join $root rtl *.sv]]
set_property include_dirs [file join $root rtl] [get_filesets sources_1]
add_files -fileset sim_1 [file join $root sim tb_pfo3_r25.sv]
set_property top tb_pfo3_r25 [get_filesets sim_1]
set_property target_simulator XSim [current_project]
set_property xsim.simulate.runtime 0ns [get_filesets sim_1]
update_compile_order -fileset sources_1
update_compile_order -fileset sim_1
puts "R25_PREFLIGHT part=[get_property PART [current_project]] simulator=[get_property target_simulator [current_project]] top=[get_property TOP [get_filesets sim_1]]"
launch_simulation -scripts_only
