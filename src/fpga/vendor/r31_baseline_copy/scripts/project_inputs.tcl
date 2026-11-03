# Exact project inputs from V9 XPR; paths relative to PL_V9/source_tree.
set rtl_files [list \
  [file join $tree {pl/build/full_project_r25/p2_board_top.sv}] \
  [file join $tree {pl/rtl/cdc/row_transaction_cdc.sv}] \
  [file join $tree {pl/rtl/p2/camera_signal_monitor.sv}] \
  [file join $tree {pl/rtl/camera/mt9v034_gray_rx.sv}] \
  [file join $tree {pl/rtl/p2/mt9v034_sccb_master.sv}] \
  [file join $tree {pl/rtl/p2/p2_async_fifo.sv}] \
  [file join $tree {pl/rtl/p2/p2_capture_core.sv}] \
  [file join $tree {pl/rtl/p2/p2_capture_metadata.sv}] \
  [file join $tree {pl/rtl/p2/p2_processing_ip.sv}] \
  [file join $tree {pl/rtl/p2/pclk_frequency_meter.sv}] \
  [file join $tree {pl/rtl/pmc2_stream_r22.sv}] \
  [file join $tree {pl/rtl/profile/r14_primary_segment_finalizer.sv}] \
  [file join $tree {pl/rtl/raw_integrity_r24.sv}] \
  [file join $tree {pl/rtl/algorithm/row_centroid_gray.sv}] \
  [file join $tree {pl/rtl/algorithm/row_unique_run_gray.sv}] \
  [file join $tree {pl/rtl/profile/slc1_compact_packer.sv}] \
  [file join $tree {pl/rtl/profile/slc3_segment_packer.sv}] \
  [file join $tree {pl/rtl/profile/slc4_selected_run_packer.sv}] \
  [file join $tree {pl/rtl/profile/slf1_feature_packer.sv}] \
  [file join $tree {pl/rtl/profile/slp2_profile_packer.sv}] \
  [file join $tree {pl/integration/p2_processing_bd.v}] \
  [file join $tree {pl/rtl/video/axis_bgrx_to_rgb24.sv}] \
  [file join $tree {pl/build/full_project_r25/axis_bgrx_to_rgb24_bd_wrapper.v}] \
  [file join $tree {pl/rtl/motion/motor_control.sv}] \
  [file join $tree {pl/rtl/motion/motor_control_bd.v}] \
  [file join $tree {pl/rtl/motion/b_motion_guard.sv}] \
  [file join $tree {pl/rtl/motion/b_motion_guard_bd.v}] \
  [file join $tree {pl/integration/axi_packet_controller.sv}] \
  [file join $tree {pl/r25_next/rtl/pfo3_banked_ram.sv}] \
  [file join $tree {pl/r25_next/rtl/pfo3_frontend_ports.svh}] \
  [file join $tree {pl/r25_next/rtl/pixel_frontend.sv}] \
  [file join $tree {pl/r25_next/rtl/pfo3_row_r25.sv}] \
  [file join $tree {pl/r25_next/rtl/pfo3_tdp_ram.sv}] \
  [file join $tree {pl/integration/pfo3_axi_bd.v}] \
  [file join $tree {pl/build/full_project_r25/structured_light_motion_profile_hdmi_top.sv}]]
set xdc_files [list \
  [file join $tree {pl/baseline/constraints/p2_pins.xdc}] \
  [file join $tree {pl/baseline/constraints/p2_timing.xdc}] \
  [file join $tree {pl/baseline/constraints/p2_hdmi_out.xdc}] \
  [file join $tree {pl/baseline/constraints/b_motion_pins.xdc}]]
