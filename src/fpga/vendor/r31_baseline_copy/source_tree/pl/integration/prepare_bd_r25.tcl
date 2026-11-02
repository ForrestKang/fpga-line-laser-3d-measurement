set root D:/Workspace/CodexWork/scan3d_pixel_frontend_20261021_25
set ::env(TEMP) D:/Workspace/CodexTemp/scan3d_r25
set ::env(TMP) $::env(TEMP)
file mkdir $::env(TEMP)
open_project $root/pl/build/full_project_r25/sl_profile_hdmi.xpr
set replacements 0
set rtl_map [dict create]
foreach f [glob -types f $root/pl/rtl/*.sv $root/pl/rtl/*/*.sv $root/pl/rtl/*/*.v $root/pl/integration/*.v] {
 dict set rtl_map [file tail $f] $f
}
foreach f [get_files -quiet] {
 set tail [file tail $f]
 if {[dict exists $rtl_map $tail]} {
  remove_files $f
  add_files -norecurse [dict get $rtl_map $tail]
  incr replacements
 }
}
if {$replacements<20} {error "incomplete R25 source rebinding $replacements"}
add_files -norecurse [glob $root/pl/r25/rtl/*.sv $root/pl/r25/rtl/*.svh]
add_files -norecurse $root/pl/integration/pfo3_axi_r25.sv
add_files -norecurse $root/pl/integration/pfo3_axi_bd_r25.v
set_property include_dirs $root/pl/r25/rtl [get_filesets sources_1]
foreach f [get_files -quiet *.xdc] {
 set new $root/pl/baseline/constraints/[file tail $f]
 if {[file exists $new]} {remove_files $f;add_files -fileset constrs_1 -norecurse $new}
}
update_compile_order -fileset sources_1
set bd [get_files -quiet */sl_profile_hdmi.bd]
open_bd_design $bd
update_module_reference [get_ips *processing_pipeline*]
set_property -dict [list CONFIG.COMPACT_POLICY_ENABLE 0 CONFIG.FEATURE_DMA_ENABLE 0 CONFIG.MULTI_CANDIDATE_ENABLE 0 CONFIG.RHI_ENABLE 0] [get_bd_cells processing_pipeline]
set pfo [create_bd_cell -type module -reference pfo3_axi_bd_r25 pfo3_frontend]
set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 pfo3_dma]
set_property -dict [list CONFIG.c_include_sg 1 CONFIG.c_sg_include_stscntrl_strm 0 CONFIG.c_sg_length_width 24 CONFIG.c_include_mm2s 1 CONFIG.c_include_s2mm 1 CONFIG.c_include_mm2s_dre 0 CONFIG.c_include_s2mm_dre 0 CONFIG.c_m_axis_mm2s_tdata_width 32 CONFIG.c_s_axis_s2mm_tdata_width 32 CONFIG.c_m_axi_mm2s_data_width 64 CONFIG.c_m_axi_s2mm_data_width 64 CONFIG.c_mm2s_burst_size 16 CONFIG.c_s2mm_burst_size 16] $dma
set_property CONFIG.NUM_MI 10 [get_bd_cells control_interconnect]
connect_bd_intf_net [get_bd_intf_pins control_interconnect/M08_AXI] [get_bd_intf_pins pfo3_frontend/S_AXI]
connect_bd_intf_net [get_bd_intf_pins control_interconnect/M09_AXI] [get_bd_intf_pins pfo3_dma/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins pfo3_dma/M_AXIS_MM2S] [get_bd_intf_pins pfo3_frontend/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins pfo3_frontend/M_AXIS] [get_bd_intf_pins pfo3_dma/S_AXIS_S2MM]
set mem [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 pfo3_memory]
set_property -dict [list CONFIG.NUM_SI 3 CONFIG.NUM_MI 1] $mem
connect_bd_intf_net [get_bd_intf_pins pfo3_dma/M_AXI_MM2S] [get_bd_intf_pins pfo3_memory/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins pfo3_dma/M_AXI_S2MM] [get_bd_intf_pins pfo3_memory/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins pfo3_dma/M_AXI_SG] [get_bd_intf_pins pfo3_memory/S02_AXI]
set_property CONFIG.PCW_USE_S_AXI_HP2 1 [get_bd_cells ps7]
connect_bd_intf_net [get_bd_intf_pins pfo3_memory/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP2]
set clk [get_bd_pins ps7/FCLK_CLK0]
set reset [get_bd_pins system_reset/peripheral_aresetn]
foreach p {pfo3_frontend/aclk pfo3_dma/s_axi_lite_aclk pfo3_dma/m_axi_mm2s_aclk pfo3_dma/m_axi_s2mm_aclk pfo3_dma/m_axi_sg_aclk pfo3_memory/ACLK pfo3_memory/S00_ACLK pfo3_memory/S01_ACLK pfo3_memory/S02_ACLK pfo3_memory/M00_ACLK control_interconnect/M08_ACLK control_interconnect/M09_ACLK ps7/S_AXI_HP2_ACLK} {connect_bd_net $clk [get_bd_pins $p]}
foreach p {pfo3_frontend/aresetn pfo3_dma/axi_resetn pfo3_memory/ARESETN pfo3_memory/S00_ARESETN pfo3_memory/S01_ARESETN pfo3_memory/S02_ARESETN pfo3_memory/M00_ARESETN control_interconnect/M08_ARESETN control_interconnect/M09_ARESETN} {connect_bd_net $reset [get_bd_pins $p]}
assign_bd_address
validate_bd_design
save_bd_design
generate_target all $bd
export_ip_user_files -of_objects $bd -no_script -sync -force -quiet
set_property top structured_light_motion_profile_hdmi_top [get_filesets sources_1]
update_compile_order -fileset sources_1
set out $root/evidence/full_r25
file mkdir $out
write_bd_tcl -force $out/block_design.tcl
set fd [open $out/compiled_sources.txt w]
foreach f [get_files -compile_order sources -used_in synthesis] {puts $fd $f}
close $fd
set fd [open $out/address_map.txt w]
foreach s [get_bd_addr_segs -hier] {puts $fd "$s [get_property OFFSET $s] [get_property RANGE $s]"}
close $fd
puts R25_BD_GENERATED_COMPLETE
