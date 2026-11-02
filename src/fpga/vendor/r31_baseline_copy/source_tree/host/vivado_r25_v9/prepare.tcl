set root D:/Workspace/CodexWork/scan3d_pixel_frontend_20261021_25
if {[get_property DIRECTORY [current_project]] ne "$root/pl/build/full_project_r25_v9"} {error WRONG_V9_ISOLATED_PROJECT}
close_design
foreach base {pfo3_frontend_r25.sv pfo3_row_r25.sv pfo3_tdp_ram.sv pfo3_frontend_ports.svh} {
 set old [get_files -all -quiet */$base]
 if {[llength $old]!=1} {error "unexpected source count for $base: $old"}
 remove_files $old
}
add_files -norecurse [glob $root/pl/r25_next/rtl/*.sv $root/pl/r25_next/rtl/*.svh]
set_property include_dirs $root/pl/r25_next/rtl [get_filesets sources_1]
update_compile_order -fileset sources_1
set bd [get_files -quiet */sl_profile_hdmi.bd]
open_bd_design $bd
foreach pattern {*bsg1_guard* *pfo3_frontend*} {
 set reference [get_ips -quiet $pattern]
 if {[llength $reference]!=1} {error "unexpected module reference $pattern: $reference"}
 update_module_reference $reference
}
validate_bd_design
save_bd_design
generate_target all $bd
export_ip_user_files -of_objects $bd -no_script -sync -force -quiet
set_property top structured_light_motion_profile_hdmi_top [get_filesets sources_1]
update_compile_order -fileset sources_1
set out $root/evidence/full_r25_v9
file mkdir $out
write_bd_tcl $out/block_design.tcl
set f [open $out/compiled_sources.txt w]
foreach s [get_files -compile_order sources -used_in synthesis] {puts $f $s}
close $f
puts R25_V9_ISOLATED_BD_READY
