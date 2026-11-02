`timescale 1ns/1ps

// P2 AXI-facing wrapper.  All datapath work remains in p2_capture_core;
// this module only provides the two small AXI4-Lite register windows and the
// board-facing camera pins.  The AXI clocks are expected to be synchronous
// to system_clk in the first PYNQ overlay; the explicit clock/reset inputs
// are present so the wrapper can be dropped into a normal Vivado BD.
module p2_processing_ip #(
    parameter integer FRAME_WIDTH = 752,
    parameter integer FRAME_HEIGHT = 480,
    parameter integer FV_ACTIVE = 1,
    parameter integer LV_ACTIVE = 1,
    parameter integer FIFO_DEPTH = 1024,
    parameter integer RAW_FIFO_DEPTH = 4096,
    parameter integer PROFILE_ENABLE = 1,
    parameter integer FEATURE_DMA_ENABLE = 0,
    parameter integer COMPACT_POLICY_ENABLE = 0,
    parameter integer USE_XPM = 0,
    parameter integer MULTI_CANDIDATE_ENABLE = 0,
    parameter integer RHI_ENABLE = 1,
    parameter integer TIMEOUT_CYCLES = 100000000,
    parameter [63:0] TIMESTAMP_INIT = 64'd0
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 system_clk CLK",
       X_INTERFACE_PARAMETER = "FREQ_HZ 100000000, ASSOCIATED_BUSIF M_AXIS_RAW:M_AXIS_PROFILE, ASSOCIATED_RESET system_resetn" *)
    input wire system_clk,
    input wire system_resetn,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 cam_pclk CLK",
       X_INTERFACE_PARAMETER = "FREQ_HZ 24000000" *)
    input wire cam_pclk,
    input wire cam_fv,
    input wire cam_lv,
    input wire [7:0] cam_data,
    output wire cam_reset_n,
    output wire cam_standby,
    input wire cam_scl_i,
    output wire cam_scl_o,
    output wire cam_scl_t,
    input wire cam_sda_i,
    output wire cam_sda_o,
    output wire cam_sda_t,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_control_aclk CLK",
       X_INTERFACE_PARAMETER = "FREQ_HZ 100000000, ASSOCIATED_BUSIF S_AXI_CONTROL, ASSOCIATED_RESET s_axi_control_aresetn" *)
    input wire s_axi_control_aclk,
    input wire s_axi_control_aresetn,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL AWADDR" *)
    input wire [15:0] s_axi_control_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL AWVALID" *)
    input wire s_axi_control_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL AWREADY" *)
    output wire s_axi_control_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL WDATA" *)
    input wire [31:0] s_axi_control_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL WSTRB" *)
    input wire [3:0] s_axi_control_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL WVALID" *)
    input wire s_axi_control_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL WREADY" *)
    output wire s_axi_control_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL BRESP" *)
    output wire [1:0] s_axi_control_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL BVALID" *)
    output wire s_axi_control_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL BREADY" *)
    input wire s_axi_control_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL ARADDR" *)
    input wire [15:0] s_axi_control_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL ARVALID" *)
    input wire s_axi_control_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL ARREADY" *)
    output wire s_axi_control_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL RDATA" *)
    output wire [31:0] s_axi_control_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL RRESP" *)
    output wire [1:0] s_axi_control_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL RVALID" *)
    output wire s_axi_control_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_CONTROL RREADY" *)
    input wire s_axi_control_rready,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_diag_aclk CLK",
       X_INTERFACE_PARAMETER = "FREQ_HZ 100000000, ASSOCIATED_BUSIF S_AXI_DIAG, ASSOCIATED_RESET s_axi_diag_aresetn" *)
    input wire s_axi_diag_aclk,
    input wire s_axi_diag_aresetn,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG AWADDR" *)
    input wire [15:0] s_axi_diag_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG AWVALID" *)
    input wire s_axi_diag_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG AWREADY" *)
    output wire s_axi_diag_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG WDATA" *)
    input wire [31:0] s_axi_diag_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG WSTRB" *)
    input wire [3:0] s_axi_diag_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG WVALID" *)
    input wire s_axi_diag_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG WREADY" *)
    output wire s_axi_diag_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG BRESP" *)
    output wire [1:0] s_axi_diag_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG BVALID" *)
    output wire s_axi_diag_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG BREADY" *)
    input wire s_axi_diag_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG ARADDR" *)
    input wire [15:0] s_axi_diag_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG ARVALID" *)
    input wire s_axi_diag_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG ARREADY" *)
    output wire s_axi_diag_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG RDATA" *)
    output wire [31:0] s_axi_diag_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG RRESP" *)
    output wire [1:0] s_axi_diag_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG RVALID" *)
    output wire s_axi_diag_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI_DIAG RREADY" *)
    input wire s_axi_diag_rready,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_RAW TDATA" *)
    output wire [31:0] m_axis_raw_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_RAW TKEEP" *)
    output wire [3:0] m_axis_raw_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_RAW TVALID" *)
    output wire m_axis_raw_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_RAW TREADY" *)
    input wire m_axis_raw_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_RAW TLAST" *)
    output wire m_axis_raw_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_PROFILE TDATA" *)
    output wire [31:0] m_axis_profile_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_PROFILE TKEEP" *)
    output wire [3:0] m_axis_profile_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_PROFILE TVALID" *)
    output wire m_axis_profile_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_PROFILE TREADY" *)
    input wire m_axis_profile_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS_PROFILE TLAST" *)
    output wire m_axis_profile_tlast,
    input wire [31:0] motor_accepted_id,
    input wire [31:0] motor_epoch,
    input wire [31:0] motor_emitted,
    output wire irq
);

    // AXI clocks are checked by the pre-board wrapper.  Using system_clk for
    // the register state keeps the datapath and control in one clock domain
    // and makes the offline model deterministic when the optional clock
    // ports are left tied to FCLK0.
    wire system_reset = !system_resetn;
    wire control_reset = !system_resetn | !s_axi_control_aresetn;
    wire diag_reset = !system_resetn | !s_axi_diag_aresetn;

    // R22_PMC2_REGISTERS, control offsets0x7c..0x94; idle-only writes.
    reg [31:0] multi_cfg_revision,multi_exposure_revision,multi_quality,multi_step,multi_residual,multi_config;
    reg [7:0] threshold_reg;
    reg [15:0] roi_start_x, roi_start_y, roi_end_x, roi_end_y;
    reg [9:0] min_width_reg, max_width_reg;
    reg [17:0] min_sum_reg;
    reg [7:0] min_peak_reg;
    reg [31:0] profile_count_reg, steps_profile_reg;
    reg [31:0] settle_reg, laser_delay_reg, trigger_width_reg;
    reg [31:0] steps_rev_reg, microsteps_reg, gear_num_reg, gear_den_reg;
    reg [31:0] timeout_reg, step_period_reg;
    reg start_cmd, stop_cmd, clear_cmd, home_cmd;
    reg [2:0] feature_mode_reg;
    reg segment_reset_pulse;
    reg selected_reset_pulse;
    wire selected_ready, selected_busy, selected_sticky_error;
    wire segment_ready, segment_busy, segment_sticky_error;
    reg diag_abort_pulse;
    reg home_rejected;
    reg [31:0] diag_camctrl_reg;
    wire diag_cam_reset = diag_camctrl_reg[0];
    wire diag_cam_standby = diag_camctrl_reg[1];
    reg signal_monitor_start_pulse;
    reg signal_monitor_abort_pulse;
    wire signal_monitor_busy, signal_monitor_done, signal_monitor_valid;
    wire signal_monitor_command_pending;
    wire [31:0] signal_monitor_result_id, signal_monitor_errors;
    wire [31:0] signal_monitor_fv_rises, signal_monitor_fv_falls;
    wire [31:0] signal_monitor_lv_rises, signal_monitor_lv_falls;
    wire [31:0] signal_monitor_fv_high, signal_monitor_fv_low;
    wire [31:0] signal_monitor_lv_high, signal_monitor_lv_low;
    wire [31:0] signal_monitor_line_min, signal_monitor_line_max;
    wire [31:0] signal_monitor_rows_min, signal_monitor_rows_max;
    wire [31:0] signal_monitor_frames;
    wire [31:0] signal_monitor_period_min, signal_monitor_period_max;
    wire [31:0] signal_monitor_pixel_min, signal_monitor_pixel_max;
    wire [31:0] signal_monitor_data_mask;
    wire [31:0] signal_monitor_status =
        {27'd0, signal_monitor_command_pending, signal_monitor_valid, (|signal_monitor_errors),
         signal_monitor_done, signal_monitor_busy};

    wire core_accepted, core_cancelled, core_complete, core_active;
    wire core_busy, core_ready;
    wire [7:0] core_error_reason;
    wire [15:0] core_error_flags;
    wire [31:0] core_profile_id;
    wire [15:0] core_epoch;
    wire [31:0] core_angle;
    wire [63:0] core_timestamp, core_timestamp_counter;
    wire [31:0] core_status;
    wire [31:0] core_width, core_height, core_last_line, core_last_frame;
    wire [31:0] core_frame_count, core_pclk_counter;
    wire [15:0] core_last_rows;
    wire [31:0] core_last_id, core_diag_errors;
    wire core_diag_published, core_camera_reset, core_camera_standby;
    wire [15:0] core_camera_eof_errors;
    wire core_camera_reset_event;
    wire metadata_valid, metadata_frame_seen;
    wire [31:0] metadata_request_id, metadata_motor_id, metadata_motor_epoch;
    wire [31:0] metadata_emitted, metadata_event_count;
    wire [15:0] metadata_capture_epoch;
    wire [63:0] metadata_timestamp;
    wire [31:0] core_diag_rdata;
    wire [31:0] measured_pclk_hz;
    wire measured_pclk_valid;
    reg pclk_valid_latched;
    reg [9:0] diag_mem_row;
    reg [3:0] diag_mem_word;
    wire [31:0] diag_mem_addr = ({22'd0, diag_mem_row} * 32'd10) + diag_mem_word;

    p2_capture_core #(
        .MULTI_CANDIDATE_ENABLE(MULTI_CANDIDATE_ENABLE),
        .FRAME_WIDTH(FRAME_WIDTH), .FRAME_HEIGHT(FRAME_HEIGHT),
        .FV_ACTIVE(FV_ACTIVE), .LV_ACTIVE(LV_ACTIVE),
        .FIFO_DEPTH(FIFO_DEPTH), .RAW_FIFO_DEPTH(RAW_FIFO_DEPTH),
        .PROFILE_ENABLE(PROFILE_ENABLE), .FEATURE_DMA_ENABLE(FEATURE_DMA_ENABLE),
        .COMPACT_POLICY_ENABLE(COMPACT_POLICY_ENABLE),
        .USE_XPM(USE_XPM),
        .TIMEOUT_CYCLES(TIMEOUT_CYCLES), .TIMESTAMP_INIT(TIMESTAMP_INIT)
    ) u_core (
        .system_clk(system_clk), .system_resetn(system_resetn),
        .cam_pclk(cam_pclk), .cam_fv(cam_fv), .cam_lv(cam_lv), .cam_data(cam_data),
        .capture_request(start_cmd), .stop_request(stop_cmd | diag_abort_pulse), .clear_request(clear_cmd),
        .cfg_feature_mode(feature_mode_reg),
        .cfg_feature_revision(multi_cfg_revision),.cfg_exposure_revision(multi_exposure_revision),
        .cfg_multi_quality(multi_quality),.cfg_multi_step_q12(multi_step),.cfg_multi_residual_q12(multi_residual),
        .cfg_multi_saturation(multi_config[7:0]),.cfg_multi_unsupported(multi_config[8]), .segment_reset_request(segment_reset_pulse),
        .selected_reset_request(selected_reset_pulse), .selected_ready(selected_ready),
        .selected_busy(selected_busy), .selected_sticky_error(selected_sticky_error),
        .segment_ready(segment_ready), .segment_busy(segment_busy), .segment_sticky_error(segment_sticky_error),
        .cfg_threshold(threshold_reg), .cfg_roi_x0(roi_start_x[9:0]),
        .cfg_roi_x1(roi_end_x[9:0]), .cfg_roi_y0(roi_start_y[9:0]),
        .cfg_roi_y1(roi_end_y[9:0]), .cfg_min_width(min_width_reg),
        .cfg_max_width(max_width_reg), .cfg_min_sum(min_sum_reg),
        .cfg_min_peak(min_peak_reg), .cfg_profile_count(profile_count_reg),
        .cfg_steps_per_profile(steps_profile_reg), .cfg_timeout_cycles(timeout_reg),
        .accepted(core_accepted),
        .cancelled(core_cancelled), .complete(core_complete),
        .active(core_active), .busy(core_busy), .ready(core_ready),
        .error_reason(core_error_reason), .error_flags(core_error_flags),
        .profile_id(core_profile_id), .epoch(core_epoch),
        .angle_latched(core_angle), .timestamp_latched(core_timestamp),
        .timestamp_counter(core_timestamp_counter),
        .m_axis_raw_tdata(m_axis_raw_tdata), .m_axis_raw_tkeep(m_axis_raw_tkeep),
        .m_axis_raw_tvalid(m_axis_raw_tvalid), .m_axis_raw_tready(m_axis_raw_tready),
        .m_axis_raw_tlast(m_axis_raw_tlast),
        .m_axis_profile_tdata(m_axis_profile_tdata), .m_axis_profile_tkeep(m_axis_profile_tkeep),
        .m_axis_profile_tvalid(m_axis_profile_tvalid), .m_axis_profile_tready(m_axis_profile_tready),
        .m_axis_profile_tlast(m_axis_profile_tlast), .status_bits(core_status),
        .camera_width(core_width), .camera_height(core_height),
        .last_line_pixels(core_last_line), .last_frame_lines(core_last_frame),
        .frame_count(core_frame_count), .pclk_counter(core_pclk_counter),
        .last_row_count(core_last_rows), .last_request_id(core_last_id),
        // epoch is already connected above. Do not join two output ports
        // onto one net: some simulators propagate X through the alias.
        .current_epoch(), .diagnostic_error_flags(core_diag_errors),
        .diag_mem_addr(diag_mem_addr), .diag_mem_rdata(32'd0),
        .diag_row_addr(diag_mem_row), .diag_word_addr(diag_mem_word),
        .diag_row_rdata(core_diag_rdata), .diag_published(core_diag_published),
        .camera_reset_assert(core_camera_reset), .camera_standby(core_camera_standby),
        .camera_eof_errors(core_camera_eof_errors),
        .camera_reset_event(core_camera_reset_event),
        .motor_accepted_id(motor_accepted_id), .motor_epoch(motor_epoch),
        .motor_emitted(motor_emitted), .metadata_valid(metadata_valid),
        .metadata_frame_seen(metadata_frame_seen), .metadata_request_id(metadata_request_id),
        .metadata_capture_epoch(metadata_capture_epoch), .metadata_motor_id(metadata_motor_id),
        .metadata_motor_epoch(metadata_motor_epoch), .metadata_emitted(metadata_emitted),
        .metadata_timestamp(metadata_timestamp), .metadata_event_count(metadata_event_count)
    );

    // R24 RHI1 passive observer; AXI clocks already share system_clk.
    reg integrity_enable, integrity_clear;
    reg [31:0] integrity_sequence;
    wire [31:0] integrity_status, integrity_id, integrity_count;
    wire [15:0] integrity_epoch;
    wire [255:0] integrity_digest;
    wire [7:0] integrity_min,integrity_max;
    wire [31:0] integrity_sum,integrity_saturated,integrity_cycles;
    generate if(RHI_ENABLE)begin:g_integrity
    raw_integrity_r24 u_integrity (
        .clk(system_clk),.reset(system_reset | diag_reset),
        .enable(integrity_enable),.clear(integrity_clear | clear_cmd),
        .start(core_accepted),.abort_frame(stop_cmd | diag_abort_pulse | core_cancelled | diag_cam_reset),
        .capture_complete(core_complete),.request_id(core_profile_id),.epoch(core_epoch),
        .expected_bytes(FRAME_WIDTH*FRAME_HEIGHT),
        .raw_data(m_axis_raw_tdata),.raw_keep(m_axis_raw_tkeep),
        .raw_fire(m_axis_raw_tvalid && m_axis_raw_tready),.raw_last(m_axis_raw_tlast),
        .status(integrity_status),.result_id(integrity_id),.result_epoch(integrity_epoch),
        .byte_count(integrity_count),.digest(integrity_digest),
        .pixel_min(integrity_min),.pixel_max(integrity_max),.pixel_sum(integrity_sum),
        .saturated_count(integrity_saturated),.cycles(integrity_cycles)
    );
    end else begin:g_no_integrity
      assign integrity_status=0;assign integrity_id=0;assign integrity_epoch=0;
      assign integrity_count=0;assign integrity_digest=0;assign integrity_min=0;
      assign integrity_max=0;assign integrity_sum=0;assign integrity_saturated=0;assign integrity_cycles=0;
    end endgenerate
    always @(posedge system_clk) begin
        if(system_reset || diag_reset) integrity_sequence<=0;
        else if(core_accepted && integrity_enable) integrity_sequence<=integrity_sequence+1;
    end

    // The camera clock is asynchronous to FCLK0.  The copied diagnostic
    // meter uses a Gray-coded camera counter and a synchronizer; no raw
    // cam_pclk signal is used in the AXI/register domain.
    pclk_frequency_meter #(
        .SYS_CLK_HZ(100_000_000), .WINDOW_CYCLES(1_000_000)
    ) u_pclk_meter (
        .sys_clk(system_clk), .sys_resetn(system_resetn),
        .cam_pclk(cam_pclk), .cam_resetn(cam_reset_n),
        .cam_pclk_hz(measured_pclk_hz),
        .measurement_valid(measured_pclk_valid),
        .synchronized_edge_count()
    );

    // This observer is independent of capture_core START/STOP and its FIFO
    // reset/flush protocol. It can establish FV/LV geometry before any DMA.
    camera_signal_monitor #(
        .FRAME_WIDTH(FRAME_WIDTH), .FRAME_HEIGHT(FRAME_HEIGHT),
        .SYS_CLOCK_HZ(100_000_000), .TIMEOUT_CYCLES(500_000_000),
        .TARGET_FRAMES(16)
    ) u_signal_monitor (
        .system_clk(system_clk), .system_resetn(system_resetn),
        .cam_pclk(cam_pclk), .cam_fv(cam_fv), .cam_lv(cam_lv), .cam_data(cam_data),
        .start_pulse(signal_monitor_start_pulse),
        .abort_pulse(signal_monitor_abort_pulse),
        .busy(signal_monitor_busy), .done(signal_monitor_done),
        .snapshot_valid(signal_monitor_valid),
        .command_pending(signal_monitor_command_pending),
        .result_id(signal_monitor_result_id),
        .error_flags(signal_monitor_errors),
        .fv_rises(signal_monitor_fv_rises), .fv_falls(signal_monitor_fv_falls),
        .lv_rises(signal_monitor_lv_rises), .lv_falls(signal_monitor_lv_falls),
        .fv_high_cycles(signal_monitor_fv_high), .fv_low_cycles(signal_monitor_fv_low),
        .lv_high_cycles(signal_monitor_lv_high), .lv_low_cycles(signal_monitor_lv_low),
        .line_pixels_min(signal_monitor_line_min), .line_pixels_max(signal_monitor_line_max),
        .frame_rows_min(signal_monitor_rows_min), .frame_rows_max(signal_monitor_rows_max),
        .complete_frames(signal_monitor_frames),
        .frame_period_min_pclk(signal_monitor_period_min),
        .frame_period_max_pclk(signal_monitor_period_max),
        .pixel_min(signal_monitor_pixel_min), .pixel_max(signal_monitor_pixel_max),
        .data_bit_variation_mask(signal_monitor_data_mask)
    );

    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset)
            pclk_valid_latched <= 1'b0;
        else if (!cam_reset_n)
            // CAM_CTRL reset starts a new measurement epoch.  Do not leave
            // the previous camera clock-valid indication latched through it.
            pclk_valid_latched <= 1'b0;
        else if (measured_pclk_valid)
            pclk_valid_latched <= 1'b1;
    end

    assign cam_reset_n = system_resetn && !core_camera_reset && !diag_cam_reset;
    assign cam_standby = diag_cam_standby;

    // ------------------------------------------------------------------
    // Lightweight AXI4-Lite register endpoint.  It accepts AW and W in
    // either order and permits one outstanding response/read.  All storage
    // is in system_clk, matching the stated first-overlay clock contract.
    // ------------------------------------------------------------------
    reg c_aw_hold, c_w_hold, c_bvalid, c_rvalid;
    reg [15:0] c_awaddr;
    reg [31:0] c_wdata;
    reg [3:0] c_wstrb;
    reg [31:0] c_rdata;
    reg [1:0] c_bresp_reg, c_rresp_reg;
    reg [63:0] ts_snapshot;
    reg [31:0] c_read_addr;
    reg [31:0] control_rejects;
    reg [31:0] irq_status_reg;
    reg [31:0] irq_enable_reg;
    reg done_latch;
    reg capture_fault_latch;

    // P2 has no motion generator.  Keep the mechanical step counter at zero
    // and retain its ABI locations for the future motion-enabled overlay.
    wire [31:0] mech_steps_low = 32'd0;
    wire [31:0] mech_steps_high = 32'd0;
    wire profile_done_event = core_complete;
    wire scan_done_event = 1'b0;
    wire capture_error_event = core_cancelled &&
                               ((core_error_flags != 16'd0) ||
                                (core_error_reason != 8'd0));
    wire [2:0] irq_event_bits = {capture_error_event, scan_done_event,
                                  profile_done_event};

    assign s_axi_control_awready = !c_aw_hold && !c_bvalid && !control_reset;
    assign s_axi_control_wready = !c_w_hold && !c_bvalid && !control_reset;
    assign s_axi_control_bvalid = c_bvalid;
    assign s_axi_control_bresp = c_bresp_reg;
    assign s_axi_control_arready = !c_rvalid && !control_reset;
    assign s_axi_control_rvalid = c_rvalid;
    assign s_axi_control_rresp = c_rresp_reg;
    assign s_axi_control_rdata = c_rdata;

    function [31:0] merge_wstrb;
        input [31:0] oldv;
        input [31:0] newv;
        input [3:0] st;
        integer k;
        begin
            merge_wstrb = oldv;
            for (k=0;k<4;k=k+1)
                if (st[k]) merge_wstrb[k*8 +: 8] = newv[k*8 +: 8];
        end
    endfunction

    function control_known_address;
        input [7:0] a;
        begin
            case (a)
                8'h00, 8'h04, 8'h08, 8'h0c, 8'h10, 8'h14, 8'h18,
                8'h1c, 8'h20, 8'h24, 8'h28, 8'h2c, 8'h30, 8'h34,
                8'h38, 8'h3c, 8'h40, 8'h44, 8'h48, 8'h4c, 8'h50,
                8'h54, 8'h58, 8'h5c, 8'h60, 8'h64, 8'h68, 8'h6c,
                8'h70, 8'h74, 8'h78, 8'h7c, 8'h80, 8'h84, 8'h88, 8'h8c, 8'h90, 8'h94: control_known_address = 1'b1;
                default: control_known_address = 1'b0;
            endcase
        end
    endfunction

    function control_write_address;
        input [7:0] a;
        begin
            case (a)
                8'h00, 8'h08, 8'h0c, 8'h10, 8'h14, 8'h18, 8'h1c,
                8'h20, 8'h24, 8'h28, 8'h2c, 8'h40, 8'h44, 8'h48,
                8'h4c, 8'h50, 8'h54, 8'h58, 8'h5c, 8'h64, 8'h68,
                8'h80, 8'h84, 8'h88, 8'h8c, 8'h90, 8'h94:
                    control_write_address = 1'b1;
                default: control_write_address = 1'b0;
            endcase
        end
    endfunction

    wire [31:0] c_new_value = merge_wstrb(32'd0, c_wdata, c_wstrb);

    function [31:0] control_read;
        input [15:0] a;
        begin
            case (a[7:0])
                8'h00: control_read = 32'd0;
                // STATUS ABI: busy[0], scan/profile completion[1], packet
                // formatter activity[2], and reserved motion_state[7:4].
                8'h04: begin
                    control_read = 32'd0;
                    control_read[0] = core_busy;
                    control_read[1] = done_latch;
                    control_read[2] = core_active && !core_status[5];
                end
                8'h08: control_read = {24'd0, threshold_reg};
                8'h0c: control_read = {roi_start_y, roi_start_x};
                8'h10: control_read = {roi_end_y, roi_end_x};
                8'h14: control_read = {22'd0, min_width_reg};
                8'h18: control_read = {22'd0, max_width_reg};
                8'h1c: control_read = steps_profile_reg;
                8'h20: control_read = settle_reg;
                8'h24: control_read = laser_delay_reg;
                8'h28: control_read = trigger_width_reg;
                8'h2c: control_read = profile_count_reg;
                // The register-level error ABI is deliberately different
                // from the packet's low-16 camera EOF field.
                8'h30: begin
                    control_read = 32'd0;
                    control_read[15:0] = core_camera_eof_errors;
                    control_read[17] = core_camera_reset_event;
                    control_read[18] = capture_fault_latch;
                end
                8'h34: control_read = core_angle;
                8'h38: control_read = ts_snapshot[31:0];
                8'h3c: control_read = ts_snapshot[63:32];
                8'h40: control_read = steps_rev_reg;
                8'h44: control_read = microsteps_reg;
                8'h48: control_read = gear_num_reg;
                8'h4c: control_read = gear_den_reg;
                8'h50: control_read = timeout_reg;
                8'h54: control_read = step_period_reg;
                8'h58: control_read = {14'd0, min_sum_reg};
                8'h5c: control_read = {24'd0, min_peak_reg};
                8'h60: control_read = core_profile_id;
                8'h64: control_read = irq_status_reg;
                8'h68: control_read = irq_enable_reg;
                8'h6c: control_read = mech_steps_low;
                8'h70: control_read = mech_steps_high;
                8'h74: control_read = 32'd0;
                8'h78: control_read = 32'h00010000;
                8'h7c: control_read = MULTI_CANDIDATE_ENABLE ? 32'h32434d50 : 0;
                8'h80: control_read = multi_cfg_revision;
                8'h84: control_read = multi_exposure_revision;
                8'h88: control_read = multi_quality;
                8'h8c: control_read = multi_step;
                8'h90: control_read = multi_residual;
                8'h94: control_read = multi_config;
                default: control_read = 32'd0;
            endcase
        end
    endfunction

    integer ck;
    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) begin
            c_aw_hold <= 0; c_w_hold <= 0; c_bvalid <= 0; c_rvalid <= 0;
            c_awaddr <= 0; c_wdata <= 0; c_wstrb <= 0; c_rdata <= 0;
            c_bresp_reg <= 2'b00; c_rresp_reg <= 2'b00;
            ts_snapshot <= 0; c_read_addr <= 0; control_rejects <= 0;
            irq_status_reg <= 32'd0; irq_enable_reg <= 32'd7;
            done_latch <= 1'b0; capture_fault_latch <= 1'b0;
            multi_cfg_revision<=0;multi_exposure_revision<=0;multi_quality<=32'h00080814;
            multi_step<=24576;multi_residual<=6144;multi_config<=255;
            threshold_reg <= 30; roi_start_x <= 0; roi_start_y <= 0;
            roi_end_x <= FRAME_WIDTH; roi_end_y <= FRAME_HEIGHT;
            min_width_reg <= 1; max_width_reg <= 32; min_sum_reg <= 30; min_peak_reg <= 15;
            profile_count_reg <= 1; steps_profile_reg <= 0; settle_reg <= 1000;
            laser_delay_reg <= 100; trigger_width_reg <= 10; steps_rev_reg <= 200;
            microsteps_reg <= 16; gear_num_reg <= 1; gear_den_reg <= 1;
            timeout_reg <= TIMEOUT_CYCLES; step_period_reg <= 0;
            start_cmd <= 0; stop_cmd <= 0; clear_cmd <= 0; home_cmd <= 0; home_rejected <= 0;
        end else begin
            start_cmd <= 0; stop_cmd <= 0; clear_cmd <= 0; home_cmd <= 0;

            // Events are sticky until software writes IRQ_STATUS.  ORing the
            // current event into the W1C result gives the documented
            // same-cycle event priority over a clear write.
            if (irq_event_bits != 3'd0)
                irq_status_reg <= irq_status_reg | {29'd0, irq_event_bits};
            if (core_accepted)
                done_latch <= 1'b0;
            if (core_complete)
                done_latch <= 1'b1;
            if (core_cancelled && (core_error_reason != 8'd6))
                capture_fault_latch <= 1'b1;

            if (c_aw_hold && c_w_hold && !c_bvalid) begin
                c_aw_hold <= 0; c_w_hold <= 0; c_bvalid <= 1;
                c_bresp_reg <= 2'b00;
                if ((c_awaddr[15:8] != 0) || (c_awaddr[1:0] != 0) ||
                    !control_known_address(c_awaddr[7:0]) ||
                    !control_write_address(c_awaddr[7:0])) begin
                    // Unknown, misaligned, and read-only addresses are
                    // explicit AXI SLVERRs; their low address bits must not
                    // alias another register.
                    c_bresp_reg <= 2'b10;
                    control_rejects <= control_rejects + 1'b1;
                end else if (core_busy &&
                             !((c_awaddr[7:0] == 8'h00) && c_wdata[1] &&
                               !c_wdata[0] && !c_wdata[2] && !c_wdata[3])) begin
                    // STOP is the only command deliberately accepted while
                    // the capture is busy.  It flushes both output branches.
                    c_bresp_reg <= 2'b10;
                    control_rejects <= control_rejects + 1'b1;
                end else begin
                    case (c_awaddr[7:0])
                        8'h00: begin
                            if (c_wdata[1]) stop_cmd <= 1;
                            if ((c_wdata[31:4] != 0) ||
                                (c_wdata[0] && c_wdata[1]) ||
                                c_wdata[2]) begin
                                c_bresp_reg <= 2'b10;
                                control_rejects <= control_rejects + 1'b1;
                                if (c_wdata[2]) home_rejected <= 1'b1;
                            end else begin
                                if (c_wdata[0]) start_cmd <= 1;
                                if (c_wdata[3] && !core_busy) begin
                                    clear_cmd <= 1;
                                    capture_fault_latch <= 1'b0;
                                end
                            end
                        end
                        8'h80: multi_cfg_revision<=merge_wstrb(multi_cfg_revision,c_wdata,c_wstrb);
                        8'h84: multi_exposure_revision<=merge_wstrb(multi_exposure_revision,c_wdata,c_wstrb);
                        8'h88: multi_quality<=merge_wstrb(multi_quality,c_wdata,c_wstrb);
                        8'h8c: multi_step<=merge_wstrb(multi_step,c_wdata,c_wstrb);
                        8'h90: multi_residual<=merge_wstrb(multi_residual,c_wdata,c_wstrb);
                        8'h94: multi_config<=merge_wstrb(multi_config,c_wdata,c_wstrb);
                        8'h08: threshold_reg <= merge_wstrb({24'd0,threshold_reg}, c_wdata, c_wstrb);
                        8'h0c: begin
                            roi_start_x <= merge_wstrb({roi_start_y,roi_start_x}, c_wdata, c_wstrb);
                            roi_start_y <= merge_wstrb({roi_start_y,roi_start_x}, c_wdata, c_wstrb) >> 16;
                        end
                        8'h10: begin
                            roi_end_x <= merge_wstrb({roi_end_y,roi_end_x}, c_wdata, c_wstrb);
                            roi_end_y <= merge_wstrb({roi_end_y,roi_end_x}, c_wdata, c_wstrb) >> 16;
                        end
                        8'h14: min_width_reg <= merge_wstrb({22'd0,min_width_reg}, c_wdata, c_wstrb);
                        8'h18: max_width_reg <= merge_wstrb({22'd0,max_width_reg}, c_wdata, c_wstrb);
                        8'h1c: steps_profile_reg <= merge_wstrb(steps_profile_reg, c_wdata, c_wstrb);
                        8'h20: settle_reg <= merge_wstrb(settle_reg, c_wdata, c_wstrb);
                        8'h24: laser_delay_reg <= merge_wstrb(laser_delay_reg, c_wdata, c_wstrb);
                        8'h28: trigger_width_reg <= merge_wstrb(trigger_width_reg, c_wdata, c_wstrb);
                        8'h2c: profile_count_reg <= merge_wstrb(profile_count_reg, c_wdata, c_wstrb);
                        8'h40: steps_rev_reg <= merge_wstrb(steps_rev_reg, c_wdata, c_wstrb);
                        8'h44: microsteps_reg <= merge_wstrb(microsteps_reg, c_wdata, c_wstrb);
                        8'h48: gear_num_reg <= merge_wstrb(gear_num_reg, c_wdata, c_wstrb);
                        8'h4c: gear_den_reg <= merge_wstrb(gear_den_reg, c_wdata, c_wstrb);
                        8'h50: timeout_reg <= merge_wstrb(timeout_reg, c_wdata, c_wstrb);
                        8'h54: step_period_reg <= merge_wstrb(step_period_reg, c_wdata, c_wstrb);
                        8'h58: min_sum_reg <= merge_wstrb({14'd0,min_sum_reg}, c_wdata, c_wstrb);
                        8'h5c: min_peak_reg <= merge_wstrb({24'd0,min_peak_reg}, c_wdata, c_wstrb);
                        8'h64: irq_status_reg <=
                            (irq_status_reg & ~c_new_value) |
                            {29'd0, irq_event_bits};
                        8'h68: irq_enable_reg <= merge_wstrb(irq_enable_reg, c_wdata, c_wstrb);
                        default: begin end
                    endcase
                end
            end
            if (c_bvalid && s_axi_control_bready) c_bvalid <= 0;
            if (s_axi_control_awvalid && s_axi_control_awready) begin
                c_aw_hold <= 1; c_awaddr <= s_axi_control_awaddr;
            end
            if (s_axi_control_wvalid && s_axi_control_wready) begin
                c_w_hold <= 1; c_wdata <= s_axi_control_wdata; c_wstrb <= s_axi_control_wstrb;
            end
            if (s_axi_control_arvalid && s_axi_control_arready) begin
                c_rvalid <= 1; c_read_addr <= s_axi_control_araddr;
                c_rresp_reg <= 2'b00;
                if ((s_axi_control_araddr[15:8] != 0) ||
                    (s_axi_control_araddr[1:0] != 0) ||
                    !control_known_address(s_axi_control_araddr[7:0])) begin
                    c_rresp_reg <= 2'b10;
                    c_rdata <= 32'd0;
                end else if (s_axi_control_araddr[7:0] == 8'h38) begin
                    // Return the same live LOW value that is captured for
                    // the following HIGH read; do not return stale snapshot.
                    ts_snapshot <= core_timestamp_counter;
                    c_rdata <= core_timestamp_counter[31:0];
                end else begin
                    c_rdata <= control_read(s_axi_control_araddr);
                end
            end
            if (c_rvalid && s_axi_control_rready) c_rvalid <= 0;
        end
    end

    // ------------------------------------------------------------------
    // Camera diagnostic AXI window.  SCCB fields drive the bounded master
    // below; all reads and writes remain in the system clock domain.
    // ------------------------------------------------------------------
    reg d_aw_hold, d_w_hold, d_bvalid, d_rvalid;
    reg [15:0] d_awaddr;
    reg [31:0] d_wdata;
    reg [3:0] d_wstrb;
    reg [31:0] d_rdata;
    reg [1:0] d_bresp_reg, d_rresp_reg;
    reg [7:0] sccb_addr_reg, sccb_reg_reg;
    reg [15:0] sccb_wdata_reg;
    reg [15:0] sccb_rdata_reg;
    reg [31:0] sccb_status_reg;
    reg [31:0] diag_error_reg;
    reg [31:0] d_read_addr;
    wire [31:0] d_new_value = merge_wstrb(32'd0, d_wdata, d_wstrb);
    reg [1:0] d_diag_wait;
    reg sccb_start_pulse;
    reg sccb_read_not_write;
    wire sccb_scl_drive_low, sccb_sda_drive_low;
    wire sccb_busy, sccb_done, sccb_ack_error, sccb_timeout_error;
    wire [15:0] sccb_read_data;
    wire [7:0] sccb_state_debug;
    // SCCB return lines are asynchronous inputs from the camera connector.
    // Synchronize them before the 100 MHz protocol FSM samples ACK/data;
    // the open-drain outputs remain combinational enables at the IOBUF.
    (* ASYNC_REG = "TRUE" *) reg scl_sync1, scl_sync2;
    (* ASYNC_REG = "TRUE" *) reg sda_sync1, sda_sync2;
    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) begin
            scl_sync1 <= 1'b1; scl_sync2 <= 1'b1;
            sda_sync1 <= 1'b1; sda_sync2 <= 1'b1;
        end else begin
            scl_sync1 <= cam_scl_i;
            scl_sync2 <= scl_sync1;
            sda_sync1 <= cam_sda_i;
            sda_sync2 <= sda_sync1;
        end
    end

    // SCCB is an open-drain two-wire bus.  The P2 copy uses bounded
    // clock-stretch and ACK waits so a missing camera cannot hold the PL
    // state machine forever.
    mt9v034_sccb_master #(
        .SYS_CLK_HZ(100_000_000), .I2C_HZ(100_000),
        .STRETCH_TIMEOUT_US(1000)
    ) u_sccb (
        .clk(system_clk), .resetn(system_resetn && s_axi_diag_aresetn),
        .start(sccb_start_pulse), .read_not_write(sccb_read_not_write),
        .device_address(sccb_addr_reg[6:0]),
        .register_address(sccb_reg_reg),
        .write_data(sccb_wdata_reg),
        .scl_i(scl_sync2), .sda_i(sda_sync2),
        .scl_drive_low(sccb_scl_drive_low),
        .sda_drive_low(sccb_sda_drive_low),
        .busy(sccb_busy), .done(sccb_done),
        .ack_error(sccb_ack_error), .timeout_error(sccb_timeout_error),
        .read_data(sccb_read_data), .state_debug(sccb_state_debug)
    );

    assign cam_scl_o = 1'b0;
    assign cam_scl_t = !sccb_scl_drive_low;
    assign cam_sda_o = 1'b0;
    assign cam_sda_t = !sccb_sda_drive_low;

    assign s_axi_diag_awready = !d_aw_hold && !d_bvalid && !diag_reset;
    assign s_axi_diag_wready = !d_w_hold && !d_bvalid && !diag_reset;
    assign s_axi_diag_bvalid = d_bvalid;
    assign s_axi_diag_bresp = d_bresp_reg;
    assign s_axi_diag_arready = !d_rvalid && (d_diag_wait == 0) && !diag_reset;
    assign s_axi_diag_rvalid = d_rvalid;
    assign s_axi_diag_rresp = d_rresp_reg;
    assign s_axi_diag_rdata = d_rdata;

    function [31:0] diag_read;
        input [15:0] a;
        begin
            if ((a >= 16'h1000) &&
                (a < (16'h1000 + FRAME_HEIGHT*64))) begin
                // The first ten words of every 0x40-byte row are defined;
                // six reserved words read as zero.
                if ((((a - 16'h1000) >> 2) & 4'hf) < 10)
                    diag_read = core_diag_rdata;
                else
                    diag_read = 32'd0;
            end else if (a >= 16'h0200 && a <= 16'h027c) begin
                case(a)
                    16'h0200: diag_read = RHI_ENABLE ? 32'h52484931 : 0;
                    16'h0204: diag_read = RHI_ENABLE ? 32'h00010000 : 0;
                    16'h0208: diag_read = RHI_ENABLE ? 32'h00000007 : 0;
                    16'h020c: diag_read = {31'd0,integrity_enable};
                    16'h0210: diag_read = integrity_status;
                    16'h0214: diag_read = integrity_id;
                    16'h0218: diag_read = {16'd0,integrity_epoch};
                    16'h021c: diag_read = integrity_count;
                    16'h0220: diag_read = {24'd0,integrity_min};
                    16'h0224: diag_read = {24'd0,integrity_max};
                    16'h0228: diag_read = integrity_sum;
                    16'h022c: diag_read = integrity_saturated;
                    16'h0230: diag_read = integrity_cycles;
                    16'h0234: diag_read = integrity_sequence;
                    16'h0238: diag_read = FRAME_WIDTH*FRAME_HEIGHT;
                    16'h023c: diag_read = 32'd4096;
                    16'h0240: diag_read = integrity_digest[255:224];
                    16'h0244: diag_read = integrity_digest[223:192];
                    16'h0248: diag_read = integrity_digest[191:160];
                    16'h024c: diag_read = integrity_digest[159:128];
                    16'h0250: diag_read = integrity_digest[127:96];
                    16'h0254: diag_read = integrity_digest[95:64];
                    16'h0258: diag_read = integrity_digest[63:32];
                    16'h025c: diag_read = integrity_digest[31:0];
                    16'h0260: diag_read = 32'd100000000;
                    default: diag_read=0;
                endcase
            end else if (a >= 16'h0134 && a <= 16'h016c) begin
                case (a)
                    16'h0134: diag_read = FEATURE_DMA_ENABLE ? 32'h52464431 : 32'd0;
                    16'h0138: diag_read = FEATURE_DMA_ENABLE ? 32'h00010000 : 32'd0;
                    16'h013c: diag_read = {29'd0,feature_mode_reg};
                    16'h0140: diag_read = COMPACT_POLICY_ENABLE ? 32'h52435131 : 32'd0;
                    16'h0148: diag_read = COMPACT_POLICY_ENABLE ? 32'h52504731 : 32'd0;
                    16'h014c: diag_read = COMPACT_POLICY_ENABLE ? 32'h00010000 : 32'd0;
                    16'h0144: diag_read = COMPACT_POLICY_ENABLE ? 32'h00010000 : 32'd0;
                    16'h0150: diag_read = COMPACT_POLICY_ENABLE ? 32'h52534731 : 32'd0;
                    16'h0154: diag_read = COMPACT_POLICY_ENABLE ? 32'h00010000 : 32'd0;
                    16'h0158: diag_read = {29'd0,segment_busy,segment_sticky_error,segment_ready};
                    16'h015c: diag_read = 32'd0;
                    16'h0160: diag_read = COMPACT_POLICY_ENABLE ? 32'h55524331 : 32'd0;
                    16'h0164: diag_read = COMPACT_POLICY_ENABLE ? 32'h00010000 : 32'd0;
                    16'h0168: diag_read = {29'd0,selected_busy,selected_sticky_error,selected_ready};
                    16'h016c: diag_read = 0;
                    default: diag_read = 0;
                endcase
            end else if (a >= 16'h0100 && a <= 16'h0130) begin
                case (a)
                    16'h0100: diag_read = 32'h43534d31;
                    16'h0104: diag_read = 32'h00010000;
                    16'h0108: diag_read = 32'h0000000f;
                    16'h010c: diag_read = {29'd0, core_active,
                        metadata_frame_seen, metadata_valid && !start_cmd && !core_busy};
                    16'h0110: diag_read = metadata_request_id;
                    16'h0114: diag_read = {16'd0,metadata_capture_epoch};
                    16'h0118: diag_read = metadata_motor_id;
                    16'h011c: diag_read = metadata_motor_epoch;
                    16'h0120: diag_read = metadata_emitted;
                    16'h0124: diag_read = metadata_timestamp[31:0];
                    16'h0128: diag_read = metadata_timestamp[63:32];
                    16'h012c: diag_read = metadata_event_count;
                    16'h0130: diag_read = 32'd100000000;
                    default: diag_read = 0;
                endcase
            end else begin
                case (a[7:0])
                    8'h00: diag_read = 32'h00010001;
                    8'h04: diag_read = PROFILE_ENABLE ? 32'h0000003f : 32'h00000039;
                    8'h08: diag_read = {29'd0, (core_busy | !system_resetn), pclk_valid_latched, core_ready};
                    8'h0c: diag_read = measured_pclk_hz;
                    8'h10: diag_read = core_width;
                    8'h14: diag_read = core_height;
                    8'h18: diag_read = core_last_line;
                    8'h1c: diag_read = core_last_frame;
                    8'h20: diag_read = core_frame_count;
                    8'h24: diag_read = {24'd0,sccb_addr_reg};
                    8'h28: diag_read = {24'd0,sccb_reg_reg};
                    8'h2c: diag_read = {16'd0,sccb_wdata_reg};
                    8'h30: diag_read = {16'd0,sccb_rdata_reg};
                    8'h34: diag_read = sccb_status_reg;
                    8'h38: diag_read = sccb_status_reg;
                    8'h3c: diag_read = diag_camctrl_reg;
                    8'h40: diag_read = 32'd0;
                    8'h44: diag_read = {16'd0,core_epoch};
                    8'h48: diag_read = core_last_id;
                    8'h4c: diag_read = {16'd0,core_last_rows};
                    8'h50: diag_read = core_diag_errors | diag_error_reg;
                    8'h54: diag_read = core_diag_errors | diag_error_reg;
                    8'h80: diag_read = 32'd0;
                    8'h84: diag_read = signal_monitor_status;
                    8'h88: diag_read = signal_monitor_result_id;
                    8'h8c: diag_read = signal_monitor_fv_rises;
                    8'h90: diag_read = signal_monitor_fv_falls;
                    8'h94: diag_read = signal_monitor_lv_rises;
                    8'h98: diag_read = signal_monitor_lv_falls;
                    8'h9c: diag_read = signal_monitor_fv_high;
                    8'ha0: diag_read = signal_monitor_fv_low;
                    8'ha4: diag_read = signal_monitor_lv_high;
                    8'ha8: diag_read = signal_monitor_lv_low;
                    8'hac: diag_read = signal_monitor_line_min;
                    8'hb0: diag_read = signal_monitor_line_max;
                    8'hb4: diag_read = signal_monitor_rows_min;
                    8'hb8: diag_read = signal_monitor_rows_max;
                    8'hbc: diag_read = signal_monitor_frames;
                    8'hc0: diag_read = signal_monitor_period_min;
                    8'hc4: diag_read = signal_monitor_period_max;
                    8'hc8: diag_read = signal_monitor_pixel_min;
                    8'hcc: diag_read = signal_monitor_pixel_max;
                    8'hd0: diag_read = signal_monitor_data_mask;
                    8'hd4: diag_read = signal_monitor_errors;
                    8'hd8: diag_read = 32'h00010000;
                    default: diag_read = 32'd0;
                endcase
            end
        end
    endfunction

    function diag_known_address;
        input [15:0] a;
        begin
            if ((a >= 16'h1000) &&
                (a < (16'h1000 + FRAME_HEIGHT*64)) &&
                (a[1:0] == 2'b00)) begin
                diag_known_address = 1'b1;
            end else if (a >= 16'h0200 && a <= 16'h027c && a[1:0] == 0) begin
                diag_known_address = 1'b1;
            end else if (a >= 16'h0100 && a <= 16'h016c && a[1:0] == 0) begin
                diag_known_address = 1'b1;
            end else begin
                case (a[7:0])
                    8'h00, 8'h04, 8'h08, 8'h0c, 8'h10, 8'h14, 8'h18,
                    8'h1c, 8'h20, 8'h24, 8'h28, 8'h2c, 8'h30, 8'h34,
                    8'h38, 8'h3c, 8'h40, 8'h44, 8'h48, 8'h4c, 8'h50,
                    8'h54, 8'h80, 8'h84, 8'h88, 8'h8c, 8'h90, 8'h94,
                    8'h98, 8'h9c, 8'ha0, 8'ha4, 8'ha8, 8'hac, 8'hb0,
                    8'hb4, 8'hb8, 8'hbc, 8'hc0, 8'hc4, 8'hc8, 8'hcc,
                    8'hd0, 8'hd4, 8'hd8: diag_known_address = (a[15:8] == 0) &&
                                                    (a[1:0] == 0);
                    default: diag_known_address = 1'b0;
                endcase
            end
        end
    endfunction

    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) begin
            integrity_enable <= 1'b0;
            d_aw_hold <= 0; d_w_hold <= 0; d_bvalid <= 0; d_rvalid <= 0;
            d_awaddr <= 0; d_wdata <= 0; d_wstrb <= 0; d_rdata <= 0;
            d_bresp_reg <= 2'b00; d_rresp_reg <= 2'b00;
            d_diag_wait <= 2'd0;
            feature_mode_reg <= 3'd0; segment_reset_pulse <= 1'b0; selected_reset_pulse<=0;
            sccb_addr_reg <= 8'h48; sccb_reg_reg <= 0; sccb_wdata_reg <= 0;
            sccb_rdata_reg <= 0; sccb_status_reg <= 0; diag_error_reg <= 0;
            sccb_start_pulse <= 1'b0; sccb_read_not_write <= 1'b0;
            diag_abort_pulse <= 1'b0;
            integrity_clear <= 1'b0;
            signal_monitor_start_pulse <= 1'b0;
            signal_monitor_abort_pulse <= 1'b0;
            // Hold the external sensor in reset until board software has
            // verified power and SCCB pull-ups, then writes CAM_CTRL=0.
            diag_camctrl_reg <= 32'd1; diag_mem_row <= 0; diag_mem_word <= 0;
        end else begin
            segment_reset_pulse <= 1'b0; selected_reset_pulse<=0;
            sccb_start_pulse <= 1'b0;
            diag_abort_pulse <= 1'b0;
            integrity_clear <= 1'b0;
            signal_monitor_start_pulse <= 1'b0;
            signal_monitor_abort_pulse <= 1'b0;
            if (d_diag_wait != 0) begin
                if (d_diag_wait == 2'd1) begin
                    // Recheck at response publication: another master may
                    // have accepted START/STOP after this address handshake.
                    // Never expose an unpublished or overwritten snapshot.
                    if (!PROFILE_ENABLE || core_busy || !core_diag_published ||
                        start_cmd || stop_cmd || diag_abort_pulse) begin
                        d_rdata <= 32'd0;
                        d_rresp_reg <= 2'b10;
                    end else begin
                        d_rdata <= core_diag_rdata;
                    end
                    d_rvalid <= 1'b1;
                end
                d_diag_wait <= d_diag_wait - 1'b1;
            end
            // Completion is sticky in the diagnostic status register until
            // the next command.  The master itself emits a one-cycle done.
            if (sccb_done) begin
                sccb_rdata_reg <= sccb_read_data;
                sccb_status_reg <= {28'd0, sccb_timeout_error,
                                    sccb_ack_error, 1'b1, 1'b0};
                if (sccb_ack_error) diag_error_reg[2] <= 1'b1;
                if (sccb_timeout_error) diag_error_reg[3] <= 1'b1;
            end else if (sccb_busy) begin
                sccb_status_reg[0] <= 1'b1;
                sccb_status_reg[1] <= 1'b0;
            end
            if (d_aw_hold && d_w_hold && !d_bvalid) begin
                d_aw_hold <= 0; d_w_hold <= 0; d_bvalid <= 1;
                d_bresp_reg <= 2'b00;
                if (!diag_known_address(d_awaddr) ||
                    ((d_awaddr[15:8] != 0) && d_awaddr != 16'h013c && d_awaddr != 16'h015c && d_awaddr != 16'h016c && d_awaddr != 16'h020c) || (d_awaddr[1:0] != 0) ||
                    !((d_awaddr == 16'h24) || (d_awaddr == 16'h28) ||
                      (d_awaddr == 16'h2c) || (d_awaddr == 16'h34) ||
                      (d_awaddr == 16'h3c) || (d_awaddr == 16'h40) ||
                    (d_awaddr == 16'h54) || (d_awaddr == 16'h80) ||
                      (d_awaddr == 16'h013c) || (d_awaddr == 16'h015c) || (d_awaddr == 16'h016c) || (d_awaddr == 16'h020c))) begin
                    d_bresp_reg <= 2'b10;
                end else begin
                    case (d_awaddr)
                        16'h020c: begin
                            if(d_wstrb[0]) begin
                                if(d_wdata[1]) integrity_clear<=1'b1;
                                if(!core_busy && !integrity_status[0]) integrity_enable<=d_wdata[0];
                                else if(d_wdata[0]!=integrity_enable) d_bresp_reg<=2'b10;
                            end
                        end
                        16'h013c: begin
                            if (FEATURE_DMA_ENABLE && !core_busy && !start_cmd &&
                                !stop_cmd && !diag_abort_pulse && d_wstrb[0] &&
                                ((d_wdata == 0) || (d_wdata == 1) || (d_wdata == 6 && MULTI_CANDIDATE_ENABLE) ||
                                 ((d_wdata == 2 || d_wdata == 3 || d_wdata == 4 || d_wdata == 5) && COMPACT_POLICY_ENABLE)))
                                feature_mode_reg <= d_wdata[2:0];
                            else d_bresp_reg <= 2'b10;
                        end
                        16'h015c: begin
                            if (COMPACT_POLICY_ENABLE && !core_busy && !segment_busy && !start_cmd &&
                                !stop_cmd && !diag_abort_pulse && d_wstrb[0] && d_wdata == 1)
                                segment_reset_pulse <= 1'b1;
                            else d_bresp_reg <= 2'b10;
                        end
                        16'h016c: begin
                            if (COMPACT_POLICY_ENABLE && !core_busy && !selected_busy && !start_cmd &&
                                !stop_cmd && !diag_abort_pulse && d_wstrb[0] && d_wdata == 1)
                                selected_reset_pulse <= 1'b1;
                            else d_bresp_reg <= 2'b10;
                        end
                        16'h24: sccb_addr_reg <= merge_wstrb({24'd0,sccb_addr_reg}, d_wdata, d_wstrb);
                        16'h28: sccb_reg_reg <= merge_wstrb({24'd0,sccb_reg_reg}, d_wdata, d_wstrb);
                        16'h2c: sccb_wdata_reg <= merge_wstrb({16'd0,sccb_wdata_reg}, d_wdata, d_wstrb);
                        16'h34: begin
                            // Exactly one command bit is legal.  A command
                            // while the bounded SCCB master is busy is
                            // rejected instead of being queued implicitly.
                            if ((d_wdata[0] ^ d_wdata[1]) && !sccb_busy &&
                                d_wstrb[0]) begin
                            sccb_read_not_write <= d_wdata[0];
                            sccb_start_pulse <= 1'b1;
                            sccb_status_reg <= 32'd1;
                            end else begin
                                sccb_status_reg <= {28'd0, 1'b0, 1'b1, 1'b0, 1'b0};
                                diag_error_reg[2] <= 1'b1;
                            end
                        end
                        16'h3c: begin
                            // Asserting the external sensor reset during an
                            // active capture is an explicit abort; a normal
                            // STOP never toggles CAM_CTRL.
                            if (d_wdata[0] && d_wstrb[0] && core_active)
                                diag_abort_pulse <= 1'b1;
                            diag_camctrl_reg <= merge_wstrb(diag_camctrl_reg, d_wdata, d_wstrb);
                        end
                        16'h40: if (d_wdata[0] && d_wstrb[0]) diag_abort_pulse <= 1'b1;
                        16'h54: diag_error_reg <= diag_error_reg & ~d_new_value;
                        16'h80: begin
                            if (d_wstrb[0] && (d_wdata[1:0] == 2'b01))
                                signal_monitor_start_pulse <= 1'b1;
                            else if (d_wstrb[0] && (d_wdata[1:0] == 2'b10))
                                signal_monitor_abort_pulse <= 1'b1;
                            else
                                diag_error_reg[4] <= 1'b1;
                        end
                        default: begin end
                    endcase
                end
            end
            if (d_bvalid && s_axi_diag_bready) d_bvalid <= 0;
            if (s_axi_diag_awvalid && s_axi_diag_awready) begin
                d_aw_hold <= 1; d_awaddr <= s_axi_diag_awaddr;
            end
            if (s_axi_diag_wvalid && s_axi_diag_wready) begin
                d_w_hold <= 1; d_wdata <= s_axi_diag_wdata; d_wstrb <= s_axi_diag_wstrb;
            end
            if (s_axi_diag_arvalid && s_axi_diag_arready) begin
                d_read_addr <= s_axi_diag_araddr;
                d_rresp_reg <= 2'b00;
                if (!diag_known_address(s_axi_diag_araddr)) begin
                    d_rresp_reg <= 2'b10;
                    d_rdata <= 32'd0;
                    d_rvalid <= 1'b1;
                end else begin
                if ((s_axi_diag_araddr >= 16'h1000) &&
                    (s_axi_diag_araddr < (16'h1000 + FRAME_HEIGHT*64))) begin
                    if (!PROFILE_ENABLE || core_busy || !core_diag_published ||
                        start_cmd || stop_cmd || diag_abort_pulse) begin
                        d_rresp_reg <= 2'b10;
                        d_rdata <= 32'd0;
                        d_rvalid <= 1'b1;
                    end else begin
                        diag_mem_row <= (s_axi_diag_araddr - 16'h1000) >> 6;
                        diag_mem_word <= ((s_axi_diag_araddr - 16'h1000) >> 2) & 4'hf;
                        d_diag_wait <= 2'd2;
                        d_rvalid <= 1'b0;
                    end
                end else begin
                    d_rdata <= diag_read(s_axi_diag_araddr);
                    d_rvalid <= 1'b1;
                end
                end
            end
            if (d_rvalid && s_axi_diag_rready) d_rvalid <= 0;
        end
    end

    assign irq = |(irq_status_reg & irq_enable_reg);

endmodule

