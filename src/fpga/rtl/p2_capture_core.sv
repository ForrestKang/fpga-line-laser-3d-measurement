`timescale 1ns/1ps

// P2 board-neutral capture engine.
//
// The engine deliberately keeps the camera side and the host side separate:
// gray pixels, ROW results, and the two packet streams cross only through
// asynchronous FIFOs.  It is suitable for the Icarus model used by the
// offline tests and for the XPM FIFO implementation used by Vivado.  The
// module does not contain PS, AXI, motor, or sensor configuration logic.
//
// A request is accepted in the system clock domain, then the camera domain
// waits for the next complete FV assertion before admitting a pixel.  The
// raw stream is packed four pixels per word, least significant byte first.
// A malformed frame, a FIFO overflow, or a packet protocol error aborts the
// entire transaction; neither output is allowed to be reported as complete.
module p2_capture_core #(
    parameter integer FRAME_WIDTH  = 752,
    parameter integer FRAME_HEIGHT = 480,
    parameter integer FV_ACTIVE    = 1,
    parameter integer LV_ACTIVE    = 1,
    parameter integer FIFO_DEPTH   = 1024,
    parameter integer RAW_FIFO_DEPTH = 4096,
    parameter integer PROFILE_ENABLE = 1,
    parameter integer FEATURE_DMA_ENABLE = 0,
    parameter integer COMPACT_POLICY_ENABLE = 0,
    parameter integer USE_XPM       = 0,
    parameter integer MULTI_CANDIDATE_ENABLE = 0,
    parameter integer TIMEOUT_CYCLES = 100000000,
    parameter [63:0] TIMESTAMP_INIT = 64'd0
) (
    input  wire system_clk,
    input  wire system_resetn,
    input  wire cam_pclk,
    input  wire cam_fv,
    input  wire cam_lv,
    input  wire [7:0] cam_data,

    input  wire capture_request,
    input  wire stop_request,
    input  wire clear_request,
    input  wire [2:0] cfg_feature_mode,
    input wire [31:0] cfg_feature_revision, cfg_exposure_revision,
    input wire [31:0] cfg_multi_quality, cfg_multi_step_q12, cfg_multi_residual_q12,
    input wire [7:0] cfg_multi_saturation,
    input wire cfg_multi_unsupported,
    input  wire segment_reset_request,
    output wire segment_ready, segment_busy, segment_sticky_error,
    input wire selected_reset_request,
    output wire selected_ready, selected_busy, selected_sticky_error,
    input  wire [7:0] cfg_threshold,
    input  wire [9:0] cfg_roi_x0,
    input  wire [9:0] cfg_roi_x1,
    input  wire [9:0] cfg_roi_y0,
    input  wire [9:0] cfg_roi_y1,
    input  wire [9:0] cfg_min_width,
    input  wire [9:0] cfg_max_width,
    input  wire [17:0] cfg_min_sum,
    input  wire [7:0] cfg_min_peak,
    input  wire [31:0] cfg_profile_count,
    input  wire [31:0] cfg_steps_per_profile,
    input  wire [31:0] cfg_timeout_cycles,

    output reg accepted,
    output reg cancelled,
    output reg complete,
    output reg active,
    output wire busy,
    output wire ready,
    output reg [7:0] error_reason,
    output reg [15:0] error_flags,
    output reg [31:0] profile_id,
    output reg [15:0] epoch,
    output reg [31:0] angle_latched,
    output reg [63:0] timestamp_latched,
    output reg [63:0] timestamp_counter,

    output wire [31:0] m_axis_raw_tdata,
    output wire [3:0]  m_axis_raw_tkeep,
    output wire        m_axis_raw_tvalid,
    input  wire        m_axis_raw_tready,
    output wire        m_axis_raw_tlast,
    output wire [31:0] m_axis_profile_tdata,
    output wire [3:0]  m_axis_profile_tkeep,
    output wire        m_axis_profile_tvalid,
    input  wire        m_axis_profile_tready,
    output wire        m_axis_profile_tlast,

    output wire [31:0] status_bits,
    output wire [31:0] camera_width,
    output wire [31:0] camera_height,
    output wire [31:0] last_line_pixels,
    output wire [31:0] last_frame_lines,
    output wire [31:0] frame_count,
    output wire [31:0] pclk_counter,
    output wire [15:0] last_row_count,
    output wire [31:0] last_request_id,
    output wire [15:0] current_epoch,
    output wire [31:0] diagnostic_error_flags,
    output wire [31:0] diag_mem_addr,
    input  wire [31:0] diag_mem_rdata,
    input  wire [9:0]  diag_row_addr,
    input  wire [3:0]  diag_word_addr,
    output wire [31:0] diag_row_rdata,
    output wire diag_published,
    output wire camera_reset_assert,
    output wire camera_standby,
    output wire [15:0] camera_eof_errors,
    output wire camera_reset_event,
    output wire camera_global_reset,
    input wire [31:0] motor_accepted_id, motor_epoch, motor_emitted,
    output wire metadata_valid, metadata_frame_seen,
    output wire [31:0] metadata_request_id,
    output wire [15:0] metadata_capture_epoch,
    output wire [31:0] metadata_motor_id, metadata_motor_epoch, metadata_emitted,
    output wire [63:0] metadata_timestamp,
    output wire [31:0] metadata_event_count
);

    localparam integer C_IDLE    = 2'd0;
    localparam integer C_ARM     = 2'd1;
    localparam integer C_CAPTURE = 2'd2;
    localparam integer C_DRAIN   = 2'd3;

    localparam integer ERR_TIMEOUT = 0;
    localparam integer ERR_CAMERA  = 1;
    localparam integer ERR_RAW_FIFO = 2;
    localparam integer ERR_ROW_FIFO = 3;
    localparam integer ERR_PACKET = 4;
    localparam integer ERR_LENGTH = 5;
    localparam integer ERR_STOP   = 6;
    localparam integer ERR_PROTOCOL = 7;

    wire system_reset = !system_resetn;
    reg flush_level;
    reg [7:0] flush_count;
    wire pipeline_reset = system_reset | flush_level;

    // A timestamp is a system-domain counter.  Stop/abort only flushes the
    // pipeline and therefore cannot reset this value.
    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset)
            timestamp_counter <= TIMESTAMP_INIT;
        else
            timestamp_counter <= timestamp_counter + 64'd1;
    end

    // Configuration is sampled exactly once, on acceptance.  The camera
    // mailbox remains stable for the complete transaction.
    reg [2:0] snap_feature_mode;
    // PMC2_INTEGRATION: mode6 owns a separate versioned packet, while
    // unchanged Raw bytes still cross the original FIFO and AXIS DMA.
    wire snap_multi_mode = MULTI_CANDIDATE_ENABLE && snap_feature_mode == 6;
    reg [31:0] snap_feature_revision, snap_exposure_revision, snap_multi_quality;
    reg [31:0] snap_multi_step_q12, snap_multi_residual_q12;
    reg [7:0] snap_multi_saturation;
    reg snap_multi_unsupported, multi_started;
    wire multi_raw_ready,multi_busy,multi_done,multi_fallback;
    wire [31:0] multi_data;
    wire multi_valid,multi_last;
    wire base_profile_busy,base_profile_done_pulse,base_profile_error;
    wire [31:0] base_profile_tdata;
    wire base_profile_tvalid,base_profile_tlast;
    reg [1023:0] multi_header;
    integer mh;
    wire snap_compact_mode = snap_feature_mode == 2 || snap_feature_mode == 3;
    wire snap_segment_mode = snap_feature_mode == 4;
    wire snap_selected_mode = snap_feature_mode == 5;
    wire selected_reset_level = system_reset | (selected_reset_request === 1'b1);
    wire segment_reset_level = system_reset | (segment_reset_request === 1'b1);
    reg [7:0] snap_threshold;
    reg [9:0] snap_x0, snap_x1, snap_y0, snap_y1, snap_minw, snap_maxw;
    reg [17:0] snap_sum;
    reg [7:0] snap_peak;
    reg [31:0] next_profile_id;
    reg [15:0] next_epoch;
    reg request_toggle;

    reg raw_done_seen;
    reg profile_done_seen;
    reg profile_frame_seen;
    reg have_profile_sof;
    reg [9:0] profile_rows;
    reg [9:0] camera_rows;
    reg [31:0] watchdog;

    // Profile packetizer and the published row diagnostics are declared
    // before the combinational aliases below.  Keeping these declarations
    // near the transaction state also makes older Icarus releases happy.
    reg pack_start, pack_point, pack_finish;
    reg [9:0] pack_row;
    reg [21:0] pack_xc;
    reg [7:0] pack_quality;
    reg [15:0] pack_flags;
    reg pack_good;
    reg [351:0] pack_feature_record;
    reg [42:0] pack_compact_extra;
    reg [63:0] pack_selected_extra;
    wire compact_row_ready;
    wire segment_row_ready;
    wire selected_row_ready;
    reg [15:0] pack_frame_errors;
    wire profile_busy, profile_done_pulse, profile_error;
    wire [31:0] profile_tdata;
    wire profile_tvalid, profile_tlast;
    // The diagnostic snapshot is split into ten independent single-port
    // memories.  Each row event writes one word to each bank at the same
    // row address, which lets Vivado infer block RAMs instead of building a
    // 4800-word dynamically indexed register array.  The snapshot is
    // written only for a completed frame, so no reset clear loop is needed.
    (* ram_style = "block" *) reg [31:0] diag_mem0 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem1 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem2 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem3 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem4 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem5 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem6 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem7 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem8 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem9 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem10 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem11 [0:FRAME_HEIGHT-1];
    (* ram_style = "block" *) reg [31:0] diag_mem12 [0:FRAME_HEIGHT-1];
    reg [31:0] diag_row_rdata_reg;
    reg [31:0] diag_q0, diag_q1, diag_q2, diag_q3, diag_q4;
    reg [31:0] diag_q5, diag_q6, diag_q7, diag_q8, diag_q9;
    reg [31:0] diag_q10, diag_q11, diag_q12;
    reg [3:0] diag_word_q;
    reg diag_read_valid;
    reg diag_published_reg;
    // Set only after the geometry EOF mailbox commit for the active capture.
    // A raw/profile packet completing without this event is not a complete
    // transaction: if the camera stops immediately after EOF, the system
    // watchdog must cancel rather than publish stale geometry.
    reg geometry_committed;
    reg camera_reset_event_reg;
    reg [15:0] cam_eof_errors_snapshot;
    // Protocol faults are generated while consuming the row FIFO, then
    // folded into error_flags by the single system transaction controller.
    // Keeping this event separate avoids multiple procedural drivers on the
    // AXI-visible error register.
    reg protocol_fault_event;
    wire row_fifo_rd_ready;

    // Pack the row diagnostic words with indexed assignments.  This keeps
    // the SLP2 sideband layout explicit and avoids mixed-width concatenation
    // sizing differences between Icarus and Vivado.
    function [31:0] pack_diag_quality_flags;
        input [14:0] flags;
        input        valid;
        input [7:0]  quality;
        begin
            pack_diag_quality_flags = 32'd0;
            pack_diag_quality_flags[7:0] = quality;
            pack_diag_quality_flags[22:8] = flags;
            pack_diag_quality_flags[23] = valid;
        end
    endfunction

    function [31:0] pack_diag_status;
        input [1:0] algorithm_flags;
        input       line_bad;
        input       overflow;
        begin
            pack_diag_status = 32'd0;
            pack_diag_status[1:0] = algorithm_flags;
            pack_diag_status[2] = line_bad;
            pack_diag_status[3] = overflow;
        end
    endfunction

    function [15:0] bin16_to_gray;
        input [15:0] value;
        begin
            bin16_to_gray = (value >> 1) ^ value;
        end
    endfunction

    function [15:0] gray16_to_bin;
        input [15:0] value;
        integer gi;
        begin
            gray16_to_bin[15] = value[15];
            for (gi = 14; gi >= 0; gi = gi - 1)
                gray16_to_bin[gi] = gray16_to_bin[gi+1] ^ value[gi];
        end
    endfunction

    wire raw_fifo_wr_busy, raw_fifo_rd_busy;
    wire row_fifo_wr_busy, row_fifo_rd_busy;
    wire raw_fifo_full, raw_fifo_almost_full, raw_fifo_overflow;
    wire row_fifo_full, row_fifo_almost_full, row_fifo_overflow;
    wire raw_fifo_wr_ready, raw_fifo_rd_valid;
    wire [32:0] raw_fifo_rd_data;
    wire row_fifo_wr_ready, row_fifo_rd_valid;
    wire [351:0] row_fifo_rd_data;
    // threshold: registered write-clock levels precede the standard level CDC.
    // Destination reset gating retains the original asserted busy value.
    reg raw_wr_busy_source, row_wr_busy_source;
    wire raw_wr_busy_level, row_wr_busy_level;
    wire raw_wr_busy_sync2 = system_reset | raw_wr_busy_level;
    wire row_wr_busy_sync2 = system_reset | row_wr_busy_level;
    // Synchronize the two FF reset outputs separately. Combining them before
    // the first synchronizer would recreate a combinational CDC source.
    // NOR after the equal-depth destination pipelines preserves ready timing.
    (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg cam_global_reset_sync1, cam_global_reset_sync2;
    (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg cam_flush_reset_sync1, cam_flush_reset_sync2;
    wire cam_ready_sync1 = !(cam_global_reset_sync1 | cam_flush_reset_sync1);
    wire cam_ready_sync2 = !(cam_global_reset_sync2 | cam_flush_reset_sync2);

    assign busy = active | flush_level | segment_busy | selected_busy | !cam_ready_sync2 |
                  raw_wr_busy_sync2 | raw_fifo_rd_busy |
                  (PROFILE_ENABLE ? (row_wr_busy_sync2 | row_fifo_rd_busy) : 1'b0);
    assign ready = !busy && !pipeline_reset && !segment_sticky_error && !selected_sticky_error &&
                   (cfg_profile_count == 32'd1) &&
                   (cfg_steps_per_profile == 32'd0);
    assign status_bits = {16'd0, diag_published, profile_done_seen,
                          raw_done_seen, flush_level, active, busy, ready};
    assign camera_width = FRAME_WIDTH;
    assign camera_height = FRAME_HEIGHT;
    assign last_request_id = profile_id;
    assign current_epoch = epoch;
    assign diagnostic_error_flags = {16'd0, error_flags};
    assign diag_mem_addr = ({22'd0, diag_row_addr} * 32'd10) + diag_word_addr;
    assign diag_row_rdata = diag_row_rdata_reg;
    // pipeline_reset only flushes the digital capture path.  A normal STOP
    // or FIFO recovery must not reset the external sensor; CAM_CTRL is the
    // only software-controlled sensor reset in the AXI wrapper.
    assign camera_reset_assert = system_reset;
    assign camera_standby = 1'b0;
    assign camera_eof_errors = cam_eof_errors_snapshot;
    assign camera_reset_event = camera_reset_event_reg;
    assign m_axis_raw_tkeep = 4'hf;
    assign m_axis_profile_tkeep = 4'hf;
    // A system reset/abort may finish before a stopped camera clock has
    // clocked the XPM write-domain reset. Do not expose any residual FIFO
    // word to an already armed DMA until both camera release and FIFO busy
    // have crossed back through their dedicated system-domain barriers.
    // Overall ready cannot gate this stream: active captures are busy.
    assign m_axis_raw_tvalid = raw_fifo_rd_valid && !pipeline_reset &&
                              cam_ready_sync2 && !raw_wr_busy_sync2 &&
                              !error_flags[ERR_RAW_FIFO] && (!snap_multi_mode || multi_raw_ready);
    assign m_axis_raw_tdata = raw_fifo_rd_data[31:0];
    assign m_axis_raw_tlast = raw_fifo_rd_data[32];
    assign m_axis_profile_tdata = profile_tdata;
    assign m_axis_profile_tvalid = profile_tvalid && !pipeline_reset &&
                                  cam_ready_sync2 && !row_wr_busy_sync2 &&
                                  !error_flags[ERR_PACKET];
    assign m_axis_profile_tlast = profile_tlast;

    // Global reset is an external asynchronous source; do not insert a
    // system-clock release stage. Flush is already a system-domain FF.
    // Keep the global-only 2-stage mailbox reset separate from the 4-stage
    // capture reset. Merge independently synchronized sources at destination.
    wire cam_global_reset_source = system_reset;
    wire cam_capture_reset_source = flush_level;
    wire cam_capture_global_reset, cam_capture_flush_reset;
    wire cam_reset = cam_capture_global_reset | cam_capture_flush_reset;
    generate
        if (USE_XPM) begin : g_camera_reset_xpm
            xpm_cdc_async_rst #(
                .DEST_SYNC_FF(2), .INIT_SYNC_FF(0), .RST_ACTIVE_HIGH(1)
            ) u_global_reset (
                .src_arst(cam_global_reset_source), .dest_clk(cam_pclk),
                .dest_arst(camera_global_reset)
            );
            // Global reset crosses into this clock domain only once.
            // Two local release stages extend the shared 2-stage reset to
            // the original capture-path 4-stage release, with immediate
            // assertion and no additional camera-clock latency.
            xpm_cdc_async_rst #(
                .DEST_SYNC_FF(2), .INIT_SYNC_FF(0), .RST_ACTIVE_HIGH(1)
            ) u_capture_global_reset (
                .src_arst(camera_global_reset), .dest_clk(cam_pclk),
                .dest_arst(cam_capture_global_reset)
            );
            xpm_cdc_async_rst #(
                .DEST_SYNC_FF(4), .INIT_SYNC_FF(0), .RST_ACTIVE_HIGH(1)
            ) u_capture_reset (
                .src_arst(cam_capture_reset_source), .dest_clk(cam_pclk),
                .dest_arst(cam_capture_flush_reset)
            );
        end else begin : g_camera_reset_model
            (* ASYNC_REG = "TRUE" *) reg [1:0] global_pipe = 2'b11;
            (* ASYNC_REG = "TRUE" *) reg [1:0] capture_global_pipe = 2'b11;
            (* ASYNC_REG = "TRUE" *) reg [3:0] capture_pipe = 4'b0000;
            always @(posedge cam_pclk or posedge cam_global_reset_source)
                if (cam_global_reset_source) global_pipe <= 2'b11;
                else global_pipe <= {global_pipe[0], 1'b0};
            always @(posedge cam_pclk or posedge camera_global_reset)
                if (camera_global_reset) capture_global_pipe <= 2'b11;
                else capture_global_pipe <= {capture_global_pipe[0], 1'b0};
            always @(posedge cam_pclk or posedge cam_capture_reset_source)
                if (cam_capture_reset_source) capture_pipe <= 4'b1111;
                else capture_pipe <= {capture_pipe[2:0], 1'b0};
            assign camera_global_reset = global_pipe[1];
            assign cam_capture_global_reset = capture_global_pipe[1];
            assign cam_capture_flush_reset = capture_pipe[3];
        end
    endgenerate
    wire cam_domain_ready = !cam_reset;

    // XPM FIFO wr_busy contains a combinational OR of local reset and the
    // primitive status. Register it at cam_pclk before crossing to system_clk.
    always @(posedge cam_pclk or posedge cam_reset) begin
        if (cam_reset) begin
            raw_wr_busy_source <= 1'b1;
            row_wr_busy_source <= 1'b1;
        end else begin
            raw_wr_busy_source <= raw_fifo_wr_busy;
            row_wr_busy_source <= row_fifo_wr_busy;
        end
    end
    generate
        if (USE_XPM) begin : g_write_busy_xpm
            xpm_cdc_single #(
                .DEST_SYNC_FF(2), .INIT_SYNC_FF(1), .SRC_INPUT_REG(0),
                .SIM_ASSERT_CHK(0)
            ) u_raw_busy (
                .src_clk(cam_pclk), .src_in(raw_wr_busy_source),
                .dest_clk(system_clk), .dest_out(raw_wr_busy_level)
            );
            xpm_cdc_single #(
                .DEST_SYNC_FF(2), .INIT_SYNC_FF(1), .SRC_INPUT_REG(0),
                .SIM_ASSERT_CHK(0)
            ) u_row_busy (
                .src_clk(cam_pclk), .src_in(row_wr_busy_source),
                .dest_clk(system_clk), .dest_out(row_wr_busy_level)
            );
        end else begin : g_write_busy_model
            (* ASYNC_REG = "TRUE" *) reg [1:0] raw_pipe = 2'b00;
            (* ASYNC_REG = "TRUE" *) reg [1:0] row_pipe = 2'b00;
            always @(posedge system_clk) begin
                raw_pipe <= {raw_pipe[0], raw_wr_busy_source};
                row_pipe <= {row_pipe[0], row_wr_busy_source};
            end
            assign raw_wr_busy_level = raw_pipe[1];
            assign row_wr_busy_level = row_pipe[1];
        end
    endgenerate

    wire rx_pixel, rx_line_end, rx_line_bad, rx_frame_start, rx_frame_end;
    wire [9:0] rx_x, rx_y, rx_line_y;
    wire [7:0] rx_gray;
    wire [15:0] rx_frame_errors;
    mt9v034_gray_rx #(
        .FRAME_WIDTH(FRAME_WIDTH), .FRAME_HEIGHT(FRAME_HEIGHT),
        .FV_ACTIVE(FV_ACTIVE), .LV_ACTIVE(LV_ACTIVE)
    ) u_rx (
        .clk(cam_pclk), .rst(cam_reset), .cam_fv(cam_fv), .cam_lv(cam_lv),
        .cam_data(cam_data), .pixel_valid(rx_pixel), .pixel_x(rx_x),
        .pixel_y(rx_y), .gray(rx_gray), .line_end(rx_line_end),
        .line_y(rx_line_y), .line_bad(rx_line_bad),
        .frame_start(rx_frame_start), .frame_end(rx_frame_end),
        .frame_errors(rx_frame_errors)
    );

    // Request and configuration mailbox into the camera clock domain.
    (* ASYNC_REG = "TRUE" *) reg req_sync1, req_sync2;
    reg req_seen;
    // A camera-domain reset must not reinterpret the old level of the
    // system-domain request toggle as a fresh capture.  After reset release
    // we first establish a synchronized baseline; the host cannot issue a
    // new START until cam_domain_ready has crossed back to system_clk.
    reg [1:0] req_baseline_count;
    reg [31:0] cam_profile_id;
    reg [15:0] cam_epoch;
    reg [7:0] cam_threshold, cam_peak;
    reg [9:0] cam_x0, cam_x1, cam_y0, cam_y1, cam_minw, cam_maxw;
    reg [17:0] cam_sum;
    reg [1:0] cam_state;
    reg [31:0] cam_pixels_seen;
    reg [1:0] raw_byte_index;
    reg [31:0] raw_word;
    reg raw_wr_valid;
    reg [32:0] raw_wr_data;
    reg raw_last_word_issued;
    reg raw_pending_valid;
    reg [31:0] raw_pending_data;
    reg cam_fault;
    reg cam_frame_bad;
    reg [15:0] cam_fault_flags;
    reg [15:0] cam_frame_count;
    reg [15:0] cam_frame_count_gray;
    reg [31:0] cam_line_pixels_measured;
    reg [31:0] cam_last_line_pixels_measured;
    reg [31:0] cam_last_frame_lines_measured;
    reg [31:0] cam_line_mailbox, cam_frame_lines_mailbox;
    reg [15:0] cam_eof_errors_mailbox;
    reg cam_geometry_toggle;
    // Geometry data is published first and the commit toggle is delayed by
    // one camera-clock edge.  This is a bundled-data mailbox: the system
    // domain never treats the data bus as a free-running vector synchronizer.
    reg geometry_pending_valid;
    // Frame-level camera errors are held until the same delayed commit edge.
    // Otherwise the system-domain fault synchronizer can abort and reset the
    // camera mailbox one cycle before its geometry toggle arrives, losing the
    // measured short/long final-line diagnostics.
    reg geometry_fault_pending;

    // Existing P1 ROW algorithm is intentionally reused without changes.
    wire row_valid;
    wire [9:0] row_number, row_width, row_candidates;
    wire [9:0] row_first_x, row_last_x, row_saturated;
    wire [21:0] row_xc, row_xc_all;
    wire [7:0] row_raw_peak;
    wire [9:0] row_sat250;
    wire [17:0] row_s0;
    wire [26:0] row_s1;
    wire [7:0] row_peak, row_quality;
    wire [15:0] row_flags, row_diagnostics;
    wire [2:0] row_ridge_flags;
    wire row_good, row_busy, row_overrun;
    wire capture_pixel = (cam_state == C_CAPTURE) ||
                         ((cam_state == C_ARM) && rx_frame_start);
    row_centroid_gray u_row (
        .clk(cam_pclk), .rst(cam_reset),
        .frame_start((cam_state == C_ARM) && rx_frame_start),
        .pixel_valid(capture_pixel && rx_pixel), .pixel_x(rx_x), .pixel_y(rx_y),
        .gray(rx_gray), .line_end(capture_pixel && rx_line_end),
        .line_y(rx_line_y), .line_bad(rx_line_bad),
        .threshold(cam_threshold), .roi_x0(cam_x0), .roi_x1(cam_x1),
        .roi_y0(cam_y0), .roi_y1(cam_y1), .min_width(cam_minw),
        .max_width(cam_maxw), .min_sum(cam_sum), .min_peak(cam_peak),
        .row_valid(row_valid), .row(row_number), .xc_fixed(row_xc),
        .s0(row_s0), .s1(row_s1), .peak(row_peak), .width(row_width),
        .candidate_count(row_candidates), .flags(row_flags),
        .first_support_x(row_first_x), .last_support_x(row_last_x),
        .saturated_support_count(row_saturated),
        .quality(row_quality), .valid(row_good),
        .diagnostics(row_diagnostics), .xc_all(row_xc_all),
        .roi_raw_peak(row_raw_peak), .roi_sat250_count(row_sat250), .ridge_flags(row_ridge_flags), .busy(row_busy), .overrun(row_overrun)
    );

    // Mode5 joins both camera-domain producer records before one CDC.
    // Only this mode waits for the new sideband; old0..4 retain their path.
    reg cam_selected_mode;
    wire selected_row_valid, selected_row_busy, selected_row_overrun;
    wire [9:0] selected_row_y, selected_row_count;
    wire [63:0] selected_row_record;
    reg join_legacy_valid, join_selected_valid;
    reg [351:0] join_legacy_data;
    reg [63:0] join_selected_data;
    reg [9:0] join_selected_y, join_selected_count;
    row_unique_run_gray #(.FRAME_WIDTH(FRAME_WIDTH),.FRAME_HEIGHT(FRAME_HEIGHT)) u_unique_run (
        .clk(cam_pclk),.rst(cam_reset),.enable(cam_selected_mode),
        .frame_start((cam_state==C_ARM) && rx_frame_start),
        .pixel_valid(capture_pixel && rx_pixel),.pixel_x(rx_x),.pixel_y(rx_y),.gray(rx_gray),
        .line_end(capture_pixel && rx_line_end),.line_y(rx_line_y),.line_bad(rx_line_bad),
        .threshold(cam_threshold),.min_peak(cam_peak),.min_sum(cam_sum),
        .roi_x0(cam_x0),.roi_x1(cam_x1),.roi_y0(cam_y0),.roi_y1(cam_y1),
        .min_width(cam_minw),.max_width(cam_maxw),
        .row_valid(selected_row_valid),.row_y(selected_row_y),.row_count(selected_row_count),
        .selected_record(selected_row_record),.busy(selected_row_busy),.overrun(selected_row_overrun)
    );

    // Raw frame is a 33-bit asynchronous FIFO: bit 32 is TLAST.  The depth
    // is a streaming buffer, not a frame store; normal output consumption
    // happens concurrently with camera writes.
    p2_async_fifo #(
        .DATA_WIDTH(33), .DEPTH(RAW_FIFO_DEPTH), .USE_XPM(USE_XPM)
    ) u_raw_fifo (
        .reset_wr(cam_reset), .reset_rd(pipeline_reset),
        .wr_clk(cam_pclk), .rd_clk(system_clk),
        .wr_valid(raw_wr_valid), .wr_data(raw_wr_data),
        .wr_ready(raw_fifo_wr_ready), .full(raw_fifo_full),
        .almost_full(raw_fifo_almost_full),
        .rd_ready(m_axis_raw_tready && m_axis_raw_tvalid),
        .rd_valid(raw_fifo_rd_valid), .rd_data(raw_fifo_rd_data),
        .wr_busy(raw_fifo_wr_busy), .rd_busy(raw_fifo_rd_busy),
        .overflow(raw_fifo_overflow)
    );

    // ROW events retain the P1 256-bit transaction contract.
    reg row_wr_valid;
    reg [351:0] row_wr_data;
    // point cloud b09: with PROFILE_ENABLE=0 nothing is ever written, but an XPM FIFO macro is not
    // trimmed by synthesis (10 RAMB36). Build it only when the Profile path exists; the
    // disabled branch drives the values an empty, idle FIFO would.
    generate if (PROFILE_ENABLE) begin : g_row_fifo
    p2_async_fifo #(
        .DATA_WIDTH(352), .DEPTH(FIFO_DEPTH), .USE_XPM(USE_XPM)
    ) u_row_fifo (
        .reset_wr(cam_reset), .reset_rd(pipeline_reset),
        .wr_clk(cam_pclk), .rd_clk(system_clk),
        .wr_valid(row_wr_valid && PROFILE_ENABLE), .wr_data(row_wr_data),
        .wr_ready(row_fifo_wr_ready), .full(row_fifo_full),
        .almost_full(row_fifo_almost_full),
        .rd_ready(row_fifo_rd_ready), .rd_valid(row_fifo_rd_valid),
        .rd_data(row_fifo_rd_data), .wr_busy(row_fifo_wr_busy),
        .rd_busy(row_fifo_rd_busy), .overflow(row_fifo_overflow)
    );
    end else begin : g_no_row_fifo
        assign row_fifo_wr_ready = 1'b1;
        assign row_fifo_full = 1'b0;
        assign row_fifo_almost_full = 1'b0;
        assign row_fifo_rd_valid = 1'b0;
        assign row_fifo_rd_data = 352'd0;
        assign row_fifo_wr_busy = 1'b0;
        assign row_fifo_rd_busy = 1'b0;
        assign row_fifo_overflow = 1'b0;
    end endgenerate

    function [351:0] event_base;
        input [1:0] kind;
        input [31:0] id;
        input [15:0] ep;
        begin
            event_base = 352'd0;
            event_base[1:0] = kind;
            event_base[33:2] = id;
            event_base[49:34] = ep;
        end
    endfunction

    // Camera-domain event and raw packer.  A refused write is recorded as a
    // fault; pixels are never silently re-ordered or re-used after FIFO full.
    always @(posedge cam_pclk) begin
        if (cam_reset) begin
            req_sync1 <= 1'b0; req_sync2 <= 1'b0; req_seen <= 1'b0;
            // Allow the request toggle to pass through both synchronizer
            // stages before taking the post-reset baseline.
            req_baseline_count <= 2'd3;
            cam_state <= C_IDLE; cam_profile_id <= 0; cam_epoch <= 0;
            cam_threshold <= 30; cam_peak <= 15; cam_sum <= 30;
            cam_x0 <= 0; cam_x1 <= FRAME_WIDTH; cam_y0 <= 0; cam_y1 <= FRAME_HEIGHT;
            cam_minw <= 1; cam_maxw <= 32;
            cam_pixels_seen <= 0; raw_byte_index <= 0; raw_word <= 0;
            raw_wr_valid <= 0; raw_wr_data <= 0; raw_last_word_issued <= 0;
            raw_pending_valid <= 1'b0; raw_pending_data <= 32'd0;
            row_wr_valid <= 0; row_wr_data <= 0; cam_fault <= 0;
            cam_selected_mode<=0; join_legacy_valid<=0; join_selected_valid<=0;
            join_legacy_data<=0; join_selected_data<=0; join_selected_y<=0; join_selected_count<=0;
            cam_frame_bad <= 1'b0;
            cam_fault_flags <= 0; cam_frame_count <= 0; cam_frame_count_gray <= 0;
            cam_line_pixels_measured <= 0;
            cam_last_line_pixels_measured <= 0;
            cam_last_frame_lines_measured <= 0;
            cam_line_mailbox <= 0; cam_frame_lines_mailbox <= 0;
            cam_eof_errors_mailbox <= 0;
            cam_geometry_toggle <= 1'b0;
            geometry_pending_valid <= 1'b0;
            geometry_fault_pending <= 1'b0;
        end else begin
            req_sync1 <= request_toggle;
            req_sync2 <= req_sync1;
            raw_wr_valid <= 1'b0;
            row_wr_valid <= 1'b0;

            // Give the mailbox one complete cam_pclk period to settle before
            // publishing its commit toggle.  The mailbox remains unchanged
            // until the next completed frame (and is deliberately not
            // cleared when a new request is accepted).
            if (geometry_pending_valid) begin
                cam_geometry_toggle <= ~cam_geometry_toggle;
                geometry_pending_valid <= 1'b0;
                if (geometry_fault_pending) begin
                    cam_fault <= 1'b1;
                    geometry_fault_pending <= 1'b0;
                end
            end

            if (raw_wr_valid && !raw_fifo_wr_ready)
                cam_fault <= 1'b1;
            if (row_wr_valid && !row_fifo_wr_ready && PROFILE_ENABLE)
                cam_fault <= 1'b1;
            if (raw_fifo_overflow || (row_fifo_overflow && PROFILE_ENABLE) || row_overrun || selected_row_overrun) begin
                cam_fault <= 1'b1;
                cam_fault_flags[ERR_RAW_FIFO] <= raw_fifo_overflow;
                cam_fault_flags[ERR_ROW_FIFO] <= row_fifo_overflow | row_overrun | selected_row_overrun;
            end

            if (req_baseline_count != 0) begin
                if (req_baseline_count == 2'd1) begin
                    req_seen <= req_sync2;
                    req_baseline_count <= 2'd0;
                end else begin
                    req_baseline_count <= req_baseline_count - 1'b1;
                end
            end

            if ((req_baseline_count == 0) && cam_state == C_IDLE &&
                req_sync2 != req_seen) begin
                req_seen <= req_sync2;
                cam_profile_id <= profile_id;
                cam_epoch <= epoch;
                cam_threshold <= snap_threshold;
                cam_peak <= snap_peak;
                cam_sum <= snap_sum;
                cam_x0 <= snap_x0; cam_x1 <= snap_x1;
                cam_y0 <= snap_y0; cam_y1 <= snap_y1;
                cam_minw <= snap_minw; cam_maxw <= snap_maxw;
                cam_selected_mode <= snap_selected_mode;
                join_legacy_valid<=0; join_selected_valid<=0;
                cam_pixels_seen <= 0; raw_byte_index <= 0; raw_word <= 0;
                raw_last_word_issued <= 0;
                raw_pending_valid <= 1'b0;
                cam_fault <= 0; cam_fault_flags <= 0;
                cam_frame_bad <= 1'b0;
                geometry_fault_pending <= 1'b0;
                cam_line_pixels_measured <= 0;
                cam_last_line_pixels_measured <= 0;
                cam_last_frame_lines_measured <= 0;
                cam_state <= C_ARM;
            end

            if (cam_state == C_ARM && rx_frame_start) begin
                cam_state <= C_CAPTURE;
                cam_pixels_seen <= 0;
                raw_byte_index <= 0;
                raw_word <= 0;
                raw_last_word_issued <= 0;
                raw_pending_valid <= 1'b0;
                cam_line_pixels_measured <= 0;
                if (PROFILE_ENABLE) begin
                    row_wr_valid <= 1'b1;
                    row_wr_data <= event_base(2'd0, cam_profile_id, cam_epoch);
                end
            end

            if (capture_pixel && rx_pixel) begin
                cam_pixels_seen <= cam_pixels_seen + 1'b1;
                cam_line_pixels_measured <= cam_line_pixels_measured + 1'b1;
                case (raw_byte_index)
                    2'd0: begin
                        raw_word[7:0] <= rx_gray;
                        raw_byte_index <= 2'd1;
                    end
                    2'd1: begin
                        raw_word[15:8] <= rx_gray;
                        raw_byte_index <= 2'd2;
                    end
                    2'd2: begin
                        raw_word[23:16] <= rx_gray;
                        raw_byte_index <= 2'd3;
                    end
                    default: begin
                        // Hold the final word until FRAME_END validates the
                        // complete FV/LV geometry.  This prevents an early
                        // TLAST from making an overlong or short frame look
                        // like a successful DMA transfer.
                        if ((rx_x == FRAME_WIDTH-1) &&
                            (rx_y == FRAME_HEIGHT-1)) begin
                            raw_pending_valid <= 1'b1;
                            raw_pending_data <= {rx_gray, raw_word[23:0]};
                            raw_last_word_issued <= 1'b1;
                        end else begin
                            raw_wr_valid <= 1'b1;
                            raw_wr_data <= {1'b0, rx_gray, raw_word[23:0]};
                        end
                        raw_byte_index <= 2'd0;
                    end
                endcase
            end

            if ((cam_state == C_CAPTURE || cam_state == C_DRAIN) && rx_line_end) begin
                // rx_line_end follows the final active pixel, so the
                // measured counter is the complete observed LV width.
                cam_last_line_pixels_measured <= cam_line_pixels_measured;
                cam_last_frame_lines_measured <= rx_line_y + 1'b1;
                cam_line_pixels_measured <= 0;
                if (rx_line_bad)
                    cam_frame_bad <= 1'b1;
            end

            // The last line's divider result is allowed to arrive after FV
            // falls, while the camera FSM is draining.  It is still part of
            // the same frame and must precede the EOF transaction.
            // Commit both producers only when source row and count agree.
            if (cam_selected_mode && join_legacy_valid && join_selected_valid) begin
                if (join_legacy_data[59:50]!=join_selected_y ||
                    join_legacy_data[154:145]!=join_selected_count) begin
                    cam_fault<=1; cam_fault_flags[ERR_ROW_FIFO]<=1;
                end else begin
                    row_wr_valid<=1;
                    row_wr_data<={join_selected_data,join_legacy_data[287:0]};
                end
                join_legacy_valid<=0; join_selected_valid<=0;
            end
            if (cam_selected_mode && selected_row_valid && PROFILE_ENABLE &&
                (cam_state==C_CAPTURE || cam_state==C_DRAIN)) begin
                if (join_selected_valid) begin cam_fault<=1; cam_fault_flags[ERR_ROW_FIFO]<=1; end
                join_selected_valid<=1; join_selected_data<=selected_row_record;
                join_selected_y<=selected_row_y; join_selected_count<=selected_row_count;
            end
            if ((cam_state == C_CAPTURE || cam_state == C_DRAIN) && row_valid && PROFILE_ENABLE) begin
                if (cam_selected_mode) begin
                    if (join_legacy_valid) begin cam_fault<=1; cam_fault_flags[ERR_ROW_FIFO]<=1; end
                join_legacy_valid <= 1'b1;
                join_legacy_data <= event_base(2'd1, cam_profile_id, cam_epoch);
                join_legacy_data[59:50] <= row_number;
                join_legacy_data[81:60] <= row_xc;
                join_legacy_data[99:82] <= row_s0;
                join_legacy_data[126:100] <= row_s1;
                join_legacy_data[134:127] <= row_peak;
                join_legacy_data[144:135] <= row_width;
                join_legacy_data[154:145] <= row_candidates;
                join_legacy_data[170:155] <= row_flags;
                join_legacy_data[178:171] <= row_quality;
                join_legacy_data[179] <= row_good;
                join_legacy_data[195:180] <= rx_frame_errors;
                join_legacy_data[211:196] <= row_diagnostics;
                join_legacy_data[221:212] <= row_first_x;
                join_legacy_data[231:222] <= row_last_x;
                join_legacy_data[241:232] <= row_saturated;
                join_legacy_data[263:242] <= row_xc_all;
                join_legacy_data[271:264] <= row_raw_peak;
                join_legacy_data[281:272] <= row_sat250;
                join_legacy_data[284:282] <= row_ridge_flags;
                end else begin
                row_wr_valid <= 1'b1;
                row_wr_data <= event_base(2'd1, cam_profile_id, cam_epoch);
                row_wr_data[59:50] <= row_number;
                row_wr_data[81:60] <= row_xc;
                row_wr_data[99:82] <= row_s0;
                row_wr_data[126:100] <= row_s1;
                row_wr_data[134:127] <= row_peak;
                row_wr_data[144:135] <= row_width;
                row_wr_data[154:145] <= row_candidates;
                row_wr_data[170:155] <= row_flags;
                row_wr_data[178:171] <= row_quality;
                row_wr_data[179] <= row_good;
                row_wr_data[195:180] <= rx_frame_errors;
                row_wr_data[211:196] <= row_diagnostics;
                row_wr_data[221:212] <= row_first_x;
                row_wr_data[231:222] <= row_last_x;
                row_wr_data[241:232] <= row_saturated;
                row_wr_data[263:242] <= row_xc_all;
                row_wr_data[271:264] <= row_raw_peak;
                row_wr_data[281:272] <= row_sat250;
                row_wr_data[284:282] <= row_ridge_flags;
                end
            end

            if (cam_state == C_CAPTURE && rx_frame_end) begin
                if ((rx_frame_errors != 0) || cam_frame_bad ||
                    (cam_pixels_seen != FRAME_WIDTH*FRAME_HEIGHT) ||
                    (raw_byte_index != 0) || !raw_last_word_issued) begin
                    // Publish the measured EOF geometry before exposing the
                    // camera fault to the system domain.  The pending bit is
                    // consumed together with the delayed mailbox toggle.
                    geometry_fault_pending <= 1'b1;
                    raw_pending_valid <= 1'b0;
                    cam_fault_flags[ERR_CAMERA] <= (rx_frame_errors != 0) || cam_frame_bad;
                    cam_fault_flags[ERR_LENGTH] <= cam_frame_bad ||
                                                   (cam_pixels_seen != FRAME_WIDTH*FRAME_HEIGHT) ||
                                                   (raw_byte_index != 0) || !raw_last_word_issued;
                end
                if ((rx_frame_errors == 0) && !cam_frame_bad &&
                    (cam_pixels_seen == FRAME_WIDTH*FRAME_HEIGHT) &&
                    (raw_byte_index == 0) && raw_pending_valid &&
                    raw_fifo_wr_ready) begin
                    raw_wr_valid <= 1'b1;
                    raw_wr_data <= {1'b1, raw_pending_data};
                    raw_pending_valid <= 1'b0;
                end
                if (PROFILE_ENABLE)
                    cam_state <= C_DRAIN;
                else if ((rx_frame_errors == 0) && !cam_frame_bad &&
                         (cam_pixels_seen == FRAME_WIDTH*FRAME_HEIGHT) &&
                         (raw_byte_index == 0) && raw_pending_valid &&
                         raw_fifo_wr_ready)
                    cam_state <= C_IDLE;
                else if (rx_frame_errors == 0 && raw_pending_valid)
                    cam_state <= C_DRAIN;
                else
                    cam_state <= C_IDLE;
                cam_frame_count <= cam_frame_count + 1'b1;
                cam_frame_count_gray <= bin16_to_gray(cam_frame_count + 1'b1);
                // Publish the measured geometry through a stable mailbox.
                // If FV and LV fall on the same camera edge, rx_line_end and
                // rx_frame_end are both asserted.  In that case the current
                // line counter and line index are the complete final-line
                // measurements; cam_last_* still contain the previous line
                // because both nonblocking assignments execute together.
                if (rx_line_end) begin
                    cam_line_mailbox <= cam_line_pixels_measured;
                    cam_frame_lines_mailbox <= rx_line_y + 1'b1;
                end else begin
                    cam_line_mailbox <= cam_last_line_pixels_measured;
                    cam_frame_lines_mailbox <= cam_last_frame_lines_measured;
                end
                cam_eof_errors_mailbox <= rx_frame_errors;
                geometry_pending_valid <= 1'b1;
            end

            if (cam_state == C_DRAIN) begin
                // The final raw word is held until FRAME_END validates the
                // geometry.  This drain applies to both raw-only and full
                // profile modes; a stalled raw FIFO must not strand the
                // packet while the ROW branch is still finishing.
                if (raw_pending_valid && raw_fifo_wr_ready) begin
                    raw_wr_valid <= 1'b1;
                    raw_wr_data <= {1'b1, raw_pending_data};
                    raw_pending_valid <= 1'b0;
                    if (!PROFILE_ENABLE)
                        cam_state <= C_IDLE;
                end
                // The divider can finish a final row after FV has fallen.
                if (PROFILE_ENABLE && !row_busy && !row_valid && !row_wr_valid &&
                    !raw_pending_valid && (!cam_selected_mode ||
                    (!selected_row_busy && !selected_row_valid && !join_legacy_valid && !join_selected_valid))) begin
                    if (camera_rows == FRAME_HEIGHT && row_fifo_wr_ready) begin
                        row_wr_valid <= 1'b1;
                        row_wr_data <= event_base(2'd2, cam_profile_id, cam_epoch);
                        row_wr_data[59:50] <= FRAME_HEIGHT;
                        row_wr_data[195:180] <= rx_frame_errors;
                        cam_state <= C_IDLE;
                    end else if (camera_rows > FRAME_HEIGHT) begin
                        cam_fault <= 1'b1;
                        cam_fault_flags[ERR_LENGTH] <= 1'b1;
                        cam_state <= C_IDLE;
                    end
                end
            end
        end
    end

    p2_capture_metadata u_metadata (
        .system_clk(system_clk), .system_resetn(system_resetn), .cam_pclk(cam_pclk),
        .cam_metadata_reset(camera_global_reset),
        .cam_sof_admitted(!cam_reset && cam_state == C_ARM && rx_frame_start),
        .cam_request_id(cam_profile_id), .cam_epoch(cam_epoch),
        .request_accepted(accepted), .active(active), .cancelled(cancelled),
        .complete(complete), .request_id(profile_id), .capture_epoch(epoch),
        .motor_accepted_id(motor_accepted_id), .motor_epoch(motor_epoch),
        .motor_emitted(motor_emitted), .timestamp_counter(timestamp_counter),
        .metadata_valid(metadata_valid), .frame_seen(metadata_frame_seen),
        .snapshot_request_id(metadata_request_id),
        .snapshot_capture_epoch(metadata_capture_epoch),
        .snapshot_motor_id(metadata_motor_id), .snapshot_motor_epoch(metadata_motor_epoch),
        .snapshot_emitted(metadata_emitted), .snapshot_timestamp(metadata_timestamp),
        .event_count(metadata_event_count)
    );

    // Count camera rows in the camera domain.  This is only a diagnostic
    // count; row events themselves remain the source of truth.
    always @(posedge cam_pclk) begin
        if (cam_reset) begin
            camera_rows <= 0;
        end else if (cam_state == C_ARM && rx_frame_start) begin
            camera_rows <= 0;
        end else if (row_valid && PROFILE_ENABLE) begin
            camera_rows <= camera_rows + 1'b1;
        end
    end

    // Synchronize camera fault and counters back to the system domain.
    (* ASYNC_REG = "TRUE" *) reg cam_fault_sync1, cam_fault_sync2;
    (* ASYNC_REG = "TRUE" *) reg [15:0] cam_fault_flags_sync1, cam_fault_flags_sync2,
                               cam_fault_flags_sync3;
    reg [31:0] cam_line_pixels_sync2, cam_frame_lines_sync2;
    (* ASYNC_REG = "TRUE" *) reg [15:0] cam_frame_count_sync1, cam_frame_count_sync2;
    (* ASYNC_REG = "TRUE" *) reg geometry_toggle_sync1, geometry_toggle_sync2;
    reg geometry_toggle_seen;
    reg [31:0] geometry_line_sync1, geometry_line_sync2;
    reg [31:0] geometry_frame_sync1, geometry_frame_sync2;
    reg [15:0] geometry_eof_sync1, geometry_eof_sync2;
    // This is a one-system-clock event: geometry_toggle_seen is updated in
    // the synchronizer process below, while the transaction controller
    // consumes the old-value comparison and latches geometry_committed.
    wire geometry_commit_pending =
        geometry_toggle_sync2 != geometry_toggle_seen;
    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) begin
            cam_fault_sync1 <= 0; cam_fault_sync2 <= 0;
            cam_fault_flags_sync1 <= 0; cam_fault_flags_sync2 <= 0;
            cam_fault_flags_sync3 <= 0;
            cam_line_pixels_sync2 <= 0; cam_frame_lines_sync2 <= 0;
            cam_frame_count_sync1 <= 0; cam_frame_count_sync2 <= 0;
            geometry_toggle_sync1 <= 0; geometry_toggle_sync2 <= 0;
            geometry_toggle_seen <= 0;
            geometry_line_sync1 <= 0; geometry_line_sync2 <= 0;
            geometry_frame_sync1 <= 0; geometry_frame_sync2 <= 0;
            geometry_eof_sync1 <= 0; geometry_eof_sync2 <= 0;
            cam_eof_errors_snapshot <= 0;
            cam_global_reset_sync1 <= 1'b1; cam_global_reset_sync2 <= 1'b1;
            cam_flush_reset_sync1 <= 1'b1; cam_flush_reset_sync2 <= 1'b1;
        end else begin
            cam_fault_sync1 <= cam_fault;
            cam_fault_sync2 <= cam_fault_sync1;
            cam_fault_flags_sync1 <= cam_fault_flags;
            cam_fault_flags_sync2 <= cam_fault_flags_sync1;
            cam_fault_flags_sync3 <= cam_fault_flags_sync2;
            // Geometry is a stable mailbox committed by the camera toggle;
            // the two-stage data samples are held until that event arrives.
            geometry_line_sync1 <= cam_line_mailbox;
            geometry_line_sync2 <= geometry_line_sync1;
            geometry_frame_sync1 <= cam_frame_lines_mailbox;
            geometry_frame_sync2 <= geometry_frame_sync1;
            geometry_eof_sync1 <= cam_eof_errors_mailbox;
            geometry_eof_sync2 <= geometry_eof_sync1;
            geometry_toggle_sync1 <= cam_geometry_toggle;
            geometry_toggle_sync2 <= geometry_toggle_sync1;
            if (geometry_toggle_sync2 != geometry_toggle_seen) begin
                geometry_toggle_seen <= geometry_toggle_sync2;
                // A camera-domain flush/reset returns the toggle and mailbox
                // to their reset values.  It is not a new EOF.  Only accept
                // a geometry mailbox update while the corresponding capture
                // is still active; this preserves the last committed
                // snapshot after an abort and prevents reset zeros from
                // replacing it.
                if (active) begin
                    cam_line_pixels_sync2 <= geometry_line_sync2;
                    cam_frame_lines_sync2 <= geometry_frame_sync2;
                    cam_eof_errors_snapshot <= geometry_eof_sync2;
                end
            end
            cam_frame_count_sync1 <= cam_frame_count_gray;
            cam_frame_count_sync2 <= cam_frame_count_sync1;
            cam_global_reset_sync1 <= cam_capture_global_reset;
            cam_global_reset_sync2 <= cam_global_reset_sync1;
            cam_flush_reset_sync1 <= cam_capture_flush_reset;
            cam_flush_reset_sync2 <= cam_flush_reset_sync1;
        end
    end

    assign row_fifo_rd_ready = active && PROFILE_ENABLE && !flush_level &&
                               !cam_fault_sync2 && !profile_error &&
                               (!have_profile_sof || (snap_compact_mode ? compact_row_ready :
                                (snap_segment_mode ? segment_row_ready : (snap_selected_mode ? selected_row_ready : 1'b1))));
    wire pack_ready = m_axis_profile_tready && !pipeline_reset &&
        cam_ready_sync2 && !row_wr_busy_sync2 && !error_flags[ERR_PACKET];
    always @* begin
        multi_header=0;
        multi_header[7*32+:32]=metadata_request_id;
        multi_header[8*32+:32]={16'd0,metadata_capture_epoch};
        multi_header[9*32+:32]=snap_feature_revision;
        multi_header[10*32+:32]=snap_exposure_revision;
        multi_header[11*32+:32]=metadata_motor_id;
        multi_header[12*32+:32]=metadata_motor_epoch;
        multi_header[13*32+:32]=metadata_emitted;
        multi_header[14*32+:64]=metadata_timestamp;
        multi_header[16*32+:32]=FRAME_WIDTH;multi_header[17*32+:32]=FRAME_HEIGHT;
        multi_header[18*32+:32]=snap_x0;multi_header[19*32+:32]=snap_y0;
        multi_header[20*32+:32]=snap_x1;multi_header[21*32+:32]=snap_y1;
        multi_header[22*32+:32]=snap_threshold;multi_header[23*32+:32]=snap_minw;
        multi_header[24*32+:32]=snap_maxw;multi_header[25*32+:32]=snap_sum;
        multi_header[26*32+:32]=snap_peak;multi_header[27*32+:32]=snap_multi_saturation;
        multi_header[28*32+:32]=snap_multi_quality;multi_header[29*32+:32]=snap_multi_step_q12;
        multi_header[30*32+:32]=snap_multi_residual_q12;
    end
    always @(posedge system_clk) begin
        if(pipeline_reset) multi_started<=0;
        else if(accepted) begin
            multi_started<=0;
            snap_feature_revision<=cfg_feature_revision;snap_exposure_revision<=cfg_exposure_revision;
            snap_multi_quality<=cfg_multi_quality;snap_multi_step_q12<=cfg_multi_step_q12;
            snap_multi_residual_q12<=cfg_multi_residual_q12;snap_multi_saturation<=cfg_multi_saturation;
            snap_multi_unsupported<=cfg_multi_unsupported;
        end else if(snap_multi_mode && active && metadata_frame_seen && !multi_started) multi_started<=1;
    end
    generate if(MULTI_CANDIDATE_ENABLE) begin:g_pmc2
        pmc2_stream_r22 #(.FRAME_WIDTH(FRAME_WIDTH),.FRAME_HEIGHT(FRAME_HEIGHT)) u_pmc2(
            .clk(system_clk),.rst(pipeline_reset),.abort(flush_level),
            .start(snap_multi_mode && active && metadata_frame_seen && !multi_started),
            .config_header(multi_header),.unsupported_config(snap_multi_unsupported),
            .raw_data(m_axis_raw_tdata),.raw_valid(snap_multi_mode && m_axis_raw_tvalid && m_axis_raw_tready),
            .raw_last(m_axis_raw_tlast),.raw_ready(multi_raw_ready),.frame_fault(1'b0),
            .tdata(multi_data),.tvalid(multi_valid),.tlast(multi_last),
            .tready(pack_ready && snap_multi_mode),.busy(multi_busy),.done(multi_done),.error(multi_fallback));
    end else begin:g_no_pmc2
        assign multi_raw_ready=1;assign multi_data=0;assign multi_valid=0;assign multi_last=0;
        assign multi_busy=0;assign multi_done=0;assign multi_fallback=0;
    end endgenerate
    assign profile_busy=snap_multi_mode?multi_busy:base_profile_busy;
    assign profile_done_pulse=snap_multi_mode?multi_done:base_profile_done_pulse;
    // A PMC2 RAW_REQUIRED packet is valid completion; capture faults still
    // abort in the original controller and cannot become sameRaw proof.
    assign profile_error=snap_multi_mode?1'b0:base_profile_error;
    assign profile_tdata=snap_multi_mode?multi_data:base_profile_tdata;
    assign profile_tvalid=snap_multi_mode?multi_valid:base_profile_tvalid;
    assign profile_tlast=snap_multi_mode?multi_last:base_profile_tlast;

    wire legacy_busy, legacy_done, legacy_error;
    wire [31:0] legacy_data;
    wire legacy_valid, legacy_last;
    slp2_profile_packer #(.MAX_POINTS(FRAME_HEIGHT)) u_profile (
        .clk(system_clk), .rst(pipeline_reset),
        .start(pack_start && (snap_feature_mode == 0)), .abort(flush_level),
        .profile_id(profile_id), .angle(32'd0), .timestamp(timestamp_latched),
        .point_valid(pack_point && (snap_feature_mode == 0)),
        .point_row(pack_row), .point_xc(pack_xc),
        .point_quality(pack_quality), .point_flags(pack_flags),
        .point_good(pack_good), .finish(pack_finish && (snap_feature_mode == 0)),
        .frame_errors(pack_frame_errors), .tready(pack_ready && (snap_feature_mode == 0)),
        .busy(legacy_busy), .done(legacy_done), .tdata(legacy_data),
        .tvalid(legacy_valid), .tlast(legacy_last), .error(legacy_error)
    );
    generate if (FEATURE_DMA_ENABLE) begin : g_feature_dma
        wire feature_busy, feature_done, feature_error;
        wire [31:0] feature_data;
        wire feature_valid, feature_last;
        slf1_feature_packer #(.MAX_POINTS(FRAME_HEIGHT),
            .FRAME_WIDTH(FRAME_WIDTH), .FRAME_HEIGHT(FRAME_HEIGHT)) u_feature (
            .clk(system_clk), .rst(pipeline_reset),
            .start(pack_start && (snap_feature_mode == 1)), .abort(flush_level),
            .profile_id(profile_id), .epoch(epoch), .angle(32'd0),
            .timestamp(timestamp_latched), .threshold(snap_threshold),
            .roi_x0(snap_x0), .roi_x1(snap_x1), .roi_y0(snap_y0), .roi_y1(snap_y1),
            .min_width(snap_minw), .max_width(snap_maxw),
            .min_sum(snap_sum), .min_peak(snap_peak),
            .point_valid(pack_point && (snap_feature_mode == 1)),
            .point_record(pack_feature_record), .finish(pack_finish && (snap_feature_mode == 1)),
            .frame_errors(pack_frame_errors), .tready(pack_ready && (snap_feature_mode == 1)),
            .busy(feature_busy), .done(feature_done), .tdata(feature_data),
            .tvalid(feature_valid), .tlast(feature_last), .error(feature_error)
        );
        wire compact_busy, compact_done, compact_error;
        wire [31:0] compact_data;
        wire compact_valid, compact_last;
        if (COMPACT_POLICY_ENABLE) begin : g_compact_policy
            slc1_compact_packer #(.MAX_POINTS(FRAME_HEIGHT),
                .FRAME_WIDTH(FRAME_WIDTH), .FRAME_HEIGHT(FRAME_HEIGHT)) u_compact (
                .clk(system_clk), .rst(pipeline_reset),
                .start(pack_start && snap_compact_mode), .abort(flush_level),
                .ridge_mode(snap_feature_mode == 3),
                .profile_id(profile_id), .epoch(epoch), .angle(32'd0),
                .timestamp(timestamp_latched), .threshold(snap_threshold),
                .roi_x0(snap_x0), .roi_x1(snap_x1), .roi_y0(snap_y0), .roi_y1(snap_y1),
                .min_width(snap_minw), .max_width(snap_maxw),
                .min_sum(snap_sum), .min_peak(snap_peak),
                .point_valid(pack_point && snap_compact_mode),
                .point_record(pack_feature_record), .point_extra(pack_compact_extra),
                .finish(pack_finish && snap_compact_mode),
                .frame_errors(pack_frame_errors), .tready(pack_ready && snap_compact_mode),
                .row_ready(compact_row_ready), .busy(compact_busy), .done(compact_done),
                .tdata(compact_data), .tvalid(compact_valid), .tlast(compact_last), .error(compact_error)
            );
        end else begin : g_no_compact_policy
            assign compact_row_ready = 1'b0;
            assign compact_busy = 1'b0; assign compact_done = 1'b0;
            assign compact_error = 1'b0; assign compact_data = 32'd0;
            assign compact_valid = 1'b0; assign compact_last = 1'b0;
        end
        wire segment_pack_busy, segment_done, segment_error;
        wire [31:0] segment_data;
        wire segment_valid, segment_last;
        if (COMPACT_POLICY_ENABLE) begin : g_segment_policy
            slc3_segment_packer #(.MAX_POINTS(FRAME_HEIGHT),
                .FRAME_WIDTH(FRAME_WIDTH), .FRAME_HEIGHT(FRAME_HEIGHT)) u_segment (
                .clk(system_clk), .rst(pipeline_reset),
                .segment_reset(segment_reset_level),
                .start(pack_start && snap_segment_mode),
                .abort(flush_level && snap_segment_mode), .ridge_mode(1'b1),
                .segment_sticky_error(segment_sticky_error), .segment_busy(segment_busy),
                .profile_id(profile_id), .epoch(epoch), .angle(32'd0),
                .timestamp(timestamp_latched), .threshold(snap_threshold),
                .roi_x0(snap_x0), .roi_x1(snap_x1), .roi_y0(snap_y0), .roi_y1(snap_y1),
                .min_width(snap_minw), .max_width(snap_maxw), .min_sum(snap_sum), .min_peak(snap_peak),
                .point_valid(pack_point && snap_segment_mode), .point_record(pack_feature_record),
                .point_extra(pack_compact_extra), .finish(pack_finish && snap_segment_mode),
                .frame_errors(pack_frame_errors), .tready(pack_ready && snap_segment_mode),
                .row_ready(segment_row_ready), .busy(segment_pack_busy), .done(segment_done),
                .tdata(segment_data), .tvalid(segment_valid), .tlast(segment_last), .error(segment_error)
            );
        end else begin : g_no_segment_policy
            assign segment_row_ready=1'b0; assign segment_pack_busy=1'b0;
            assign segment_done=1'b0; assign segment_error=1'b0;
            assign segment_data=32'd0; assign segment_valid=1'b0; assign segment_last=1'b0;
            assign segment_busy=1'b0; assign segment_sticky_error=1'b0;
        end
        wire selected_pack_busy, selected_done, selected_error;
        wire [31:0] selected_data;
        wire selected_valid, selected_last;
        if (COMPACT_POLICY_ENABLE) begin : g_selected_run_policy
            slc4_selected_run_packer #(.MAX_POINTS(FRAME_HEIGHT),
                .FRAME_WIDTH(FRAME_WIDTH),.FRAME_HEIGHT(FRAME_HEIGHT)) u_selected (
                .clk(system_clk),.rst(pipeline_reset),.segment_reset(selected_reset_level),
                .start(pack_start && snap_selected_mode),.abort(flush_level && snap_selected_mode),
                .ridge_mode(1'b1),.segment_sticky_error(selected_sticky_error),.segment_busy(selected_busy),
                .profile_id(profile_id),.epoch(epoch),.angle(32'd0),.timestamp(timestamp_latched),
                .threshold(snap_threshold),.min_peak(snap_peak),.min_sum(snap_sum),
                .roi_x0(snap_x0),.roi_x1(snap_x1),.roi_y0(snap_y0),.roi_y1(snap_y1),
                .min_width(snap_minw),.max_width(snap_maxw),
                .point_valid(pack_point && snap_selected_mode),.point_record(pack_feature_record),
                .point_extra(pack_compact_extra),.point_selected(pack_selected_extra),
                .finish(pack_finish && snap_selected_mode),.frame_errors(pack_frame_errors),
                .tready(pack_ready && snap_selected_mode),.row_ready(selected_row_ready),
                .busy(selected_pack_busy),.done(selected_done),.tdata(selected_data),
                .tvalid(selected_valid),.tlast(selected_last),.error(selected_error)
            );
        end else begin : g_no_selected_run_policy
            assign selected_row_ready=0; assign selected_pack_busy=0; assign selected_done=0;
            assign selected_error=0; assign selected_data=0; assign selected_valid=0; assign selected_last=0;
            assign selected_busy=0; assign selected_sticky_error=0;
        end
        assign base_profile_busy = snap_selected_mode ? selected_pack_busy : (snap_segment_mode ? segment_pack_busy : (snap_compact_mode ? compact_busy : (snap_feature_mode == 1 ? feature_busy : legacy_busy)));
        assign base_profile_done_pulse = snap_selected_mode ? selected_done : (snap_segment_mode ? segment_done : (snap_compact_mode ? compact_done : (snap_feature_mode == 1 ? feature_done : legacy_done)));
        assign base_profile_error = snap_selected_mode ? (selected_error | selected_sticky_error) : (snap_segment_mode ? (segment_error | segment_sticky_error) : (snap_compact_mode ? compact_error : (snap_feature_mode == 1 ? feature_error : legacy_error)));
        assign base_profile_tdata = snap_selected_mode ? selected_data : (snap_segment_mode ? segment_data : (snap_compact_mode ? compact_data : (snap_feature_mode == 1 ? feature_data : legacy_data)));
        assign base_profile_tvalid = snap_selected_mode ? selected_valid : (snap_segment_mode ? segment_valid : (snap_compact_mode ? compact_valid : (snap_feature_mode == 1 ? feature_valid : legacy_valid)));
        assign base_profile_tlast = snap_selected_mode ? selected_last : (snap_segment_mode ? segment_last : (snap_compact_mode ? compact_last : (snap_feature_mode == 1 ? feature_last : legacy_last)));
    end else begin : g_legacy_dma
        assign segment_row_ready=1'b0; assign segment_busy=1'b0; assign segment_sticky_error=1'b0;
        assign selected_row_ready=0; assign selected_busy=0; assign selected_sticky_error=0;
        assign base_profile_busy = legacy_busy;
        assign base_profile_done_pulse = legacy_done;
        assign base_profile_error = legacy_error;
        assign base_profile_tdata = legacy_data;
        assign base_profile_tvalid = legacy_valid;
        assign base_profile_tlast = legacy_last;
    end endgenerate

    assign segment_ready = !segment_sticky_error && !segment_busy;
    assign selected_ready = !selected_sticky_error && !selected_busy;

    // Each RAM bank has a reset-free synchronous read port.  Muxing before
    // one asynchronously-reset output register prevents Vivado from inferring
    // BRAM and previously consumed ~80k FFs.  Select AFTER the bank registers
    // using the matching delayed word index: the core still has one-cycle
    // read latency, so the AXI wrapper's two-cycle response is unchanged.
    // Contents are never cleared; diag_published gates software visibility.
    wire diag_write_enable = !system_reset && PROFILE_ENABLE &&
        row_fifo_rd_valid && row_fifo_rd_ready && have_profile_sof &&
        row_fifo_rd_data[1:0] == 2'd1 &&
        row_fifo_rd_data[33:2] == profile_id &&
        row_fifo_rd_data[49:34] == epoch &&
        row_fifo_rd_data[59:50] == profile_rows && profile_rows < FRAME_HEIGHT;

    always @(posedge system_clk) begin
        if (diag_write_enable) begin
            diag_mem0[row_fifo_rd_data[59:50]] <= {22'd0,row_fifo_rd_data[59:50]};
            diag_mem1[row_fifo_rd_data[59:50]] <= {10'd0,row_fifo_rd_data[81:60]};
            diag_mem2[row_fifo_rd_data[59:50]] <= {14'd0,row_fifo_rd_data[99:82]};
            diag_mem3[row_fifo_rd_data[59:50]] <= {5'd0,row_fifo_rd_data[126:100]};
            diag_mem4[row_fifo_rd_data[59:50]] <= {24'd0,row_fifo_rd_data[134:127]};
            diag_mem5[row_fifo_rd_data[59:50]] <= {22'd0,row_fifo_rd_data[144:135]};
            diag_mem6[row_fifo_rd_data[59:50]] <= {22'd0,row_fifo_rd_data[154:145]};
            diag_mem7[row_fifo_rd_data[59:50]] <= pack_diag_quality_flags(
                row_fifo_rd_data[169:155],row_fifo_rd_data[179],row_fifo_rd_data[178:171]);
            diag_mem8[row_fifo_rd_data[59:50]] <= pack_diag_status(
                row_fifo_rd_data[197:196],row_fifo_rd_data[159],row_fifo_rd_data[160]);
            diag_mem9[row_fifo_rd_data[59:50]] <= row_fifo_rd_data[33:2];
            if (COMPACT_POLICY_ENABLE) begin
                diag_mem10[row_fifo_rd_data[59:50]] <= {2'd0,row_fifo_rd_data[241:212]};
                diag_mem11[row_fifo_rd_data[59:50]] <= {2'd0,row_fifo_rd_data[271:242]};
                diag_mem12[row_fifo_rd_data[59:50]] <= {22'd0,row_fifo_rd_data[281:272]};
            end
        end
        diag_q0 <= diag_mem0[diag_row_addr];
        diag_q1 <= diag_mem1[diag_row_addr];
        diag_q2 <= diag_mem2[diag_row_addr];
        diag_q3 <= diag_mem3[diag_row_addr];
        diag_q4 <= diag_mem4[diag_row_addr];
        diag_q5 <= diag_mem5[diag_row_addr];
        diag_q6 <= diag_mem6[diag_row_addr];
        diag_q7 <= diag_mem7[diag_row_addr];
        diag_q8 <= diag_mem8[diag_row_addr];
        diag_q9 <= diag_mem9[diag_row_addr];
        diag_q10 <= diag_mem10[diag_row_addr];
        diag_q11 <= diag_mem11[diag_row_addr];
        diag_q12 <= diag_mem12[diag_row_addr];
        diag_word_q <= diag_word_addr;
    end
    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) diag_read_valid <= 1'b0;
        else diag_read_valid <= 1'b1;
    end
    always @* begin
        diag_row_rdata_reg = 32'd0;
        if (diag_read_valid) begin
            case (diag_word_q)
                4'd0: diag_row_rdata_reg = diag_q0;
                4'd1: diag_row_rdata_reg = diag_q1;
                4'd2: diag_row_rdata_reg = diag_q2;
                4'd3: diag_row_rdata_reg = diag_q3;
                4'd4: diag_row_rdata_reg = diag_q4;
                4'd5: diag_row_rdata_reg = diag_q5;
                4'd6: diag_row_rdata_reg = diag_q6;
                4'd7: diag_row_rdata_reg = diag_q7;
                4'd8: diag_row_rdata_reg = diag_q8;
                4'd9: diag_row_rdata_reg = diag_q9;
                4'd10: if (COMPACT_POLICY_ENABLE) diag_row_rdata_reg = diag_q10;
                4'd11: if (COMPACT_POLICY_ENABLE) diag_row_rdata_reg = diag_q11;
                4'd12: if (COMPACT_POLICY_ENABLE) diag_row_rdata_reg = diag_q12;
                default: diag_row_rdata_reg = 32'd0;
            endcase
        end
    end

    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) begin
            pack_start <= 0; pack_point <= 0; pack_finish <= 0;
            pack_row <= 0; pack_xc <= 0; pack_quality <= 0; pack_flags <= 0;
            pack_good <= 0; pack_frame_errors <= 0; pack_feature_record <= 0; pack_compact_extra <= 0; pack_selected_extra<=0;
            have_profile_sof <= 0; profile_rows <= 0;
            diag_published_reg <= 0;
            protocol_fault_event <= 1'b0;
        end else begin
            pack_start <= 0; pack_point <= 0; pack_finish <= 0;
            if (accepted) begin
                have_profile_sof <= 0;
                profile_rows <= 0;
                diag_published_reg <= 0;
            end
            if (!active)
                protocol_fault_event <= 1'b0;
            if (row_fifo_rd_valid && row_fifo_rd_ready && PROFILE_ENABLE) begin
                if (row_fifo_rd_data[33:2] != profile_id ||
                    row_fifo_rd_data[49:34] != epoch) begin
                    protocol_fault_event <= 1'b1;
                end else begin
                    case (row_fifo_rd_data[1:0])
                        2'd0: begin
                            if (have_profile_sof)
                                protocol_fault_event <= 1'b1;
                            else begin
                                have_profile_sof <= 1'b1;
                                profile_rows <= 0;
                                pack_start <= 1'b1;
                            end
                        end
                        2'd1: begin
                            if (!have_profile_sof ||
                                row_fifo_rd_data[59:50] != profile_rows ||
                                profile_rows >= FRAME_HEIGHT) begin
                                protocol_fault_event <= 1'b1;
                            end else begin
                                pack_point <= 1'b1;
                                pack_row <= row_fifo_rd_data[59:50];
                                pack_xc <= row_fifo_rd_data[81:60];
                                pack_quality <= row_fifo_rd_data[178:171];
                                pack_flags <= row_fifo_rd_data[170:155];
                                pack_good <= row_fifo_rd_data[179];
                                pack_compact_extra <= row_fifo_rd_data[284:242];
                                pack_selected_extra <= row_fifo_rd_data[351:288];
                                pack_feature_record <= {
                                    2'd0, row_fifo_rd_data[241:212],
                                    row_fifo_rd_data[33:2],
                                    pack_diag_status(row_fifo_rd_data[197:196],
                                        row_fifo_rd_data[159],row_fifo_rd_data[160]),
                                    pack_diag_quality_flags(row_fifo_rd_data[169:155],
                                        row_fifo_rd_data[179],row_fifo_rd_data[178:171]),
                                    22'd0,row_fifo_rd_data[154:145],
                                    22'd0,row_fifo_rd_data[144:135],
                                    24'd0,row_fifo_rd_data[134:127],
                                    5'd0,row_fifo_rd_data[126:100],
                                    14'd0,row_fifo_rd_data[99:82],
                                    10'd0,row_fifo_rd_data[81:60],
                                    22'd0,row_fifo_rd_data[59:50]};
                                profile_rows <= profile_rows + 1'b1;
                            end
                        end
                        2'd2: begin
                            if (!have_profile_sof || row_fifo_rd_data[59:50] != profile_rows) begin
                                protocol_fault_event <= 1'b1;
                            end else begin
                                pack_finish <= 1'b1;
                                pack_frame_errors <= row_fifo_rd_data[195:180];
                                have_profile_sof <= 1'b0;
                            end
                        end
                        default: protocol_fault_event <= 1'b1;
                    endcase
                end
            end
            // Publish the diagnostic window only after both output branches
            // have actually completed.  The EOF event alone is not enough:
            // the profile packet may still be stalled by AXI backpressure.
            if (active && geometry_committed && raw_done_seen &&
                (profile_done_seen || profile_done_pulse))
                diag_published_reg <= 1'b1;
        end
    end
    // Do not expose the previous frame's diagnostic snapshot while a new
    // request is waiting for its EOF geometry commit or after an abort.
    assign diag_published = diag_published_reg && geometry_committed;

    // System-domain transaction controller.  All abort sources converge on
    // flush_level, so the two stream branches are discarded together.
    always @(posedge system_clk or posedge system_reset) begin
        if (system_reset) begin
            accepted <= 0; cancelled <= 0; complete <= 0; active <= 0;
            error_reason <= 0; error_flags <= 0; profile_id <= 0; epoch <= 0;
            angle_latched <= 0; timestamp_latched <= 0; snap_feature_mode <= 0;
            snap_threshold <= 30; snap_peak <= 15; snap_sum <= 30;
            snap_x0 <= 0; snap_x1 <= FRAME_WIDTH; snap_y0 <= 0; snap_y1 <= FRAME_HEIGHT;
            snap_minw <= 1; snap_maxw <= 32;
            next_profile_id <= 0; next_epoch <= 0; request_toggle <= 0;
            raw_done_seen <= 0; profile_done_seen <= (PROFILE_ENABLE ? 0 : 1);
            profile_frame_seen <= 0; watchdog <= 0;
            flush_level <= 0; flush_count <= 0;
            geometry_committed <= 1'b0;
            camera_reset_event_reg <= 1'b0;
        end else begin
            accepted <= 0; cancelled <= 0; complete <= 0;

            // The synchronized toggle is consumed only for the active
            // request.  The mailbox data itself is committed by the
            // synchronizer process above before this latch becomes visible.
            if (active && geometry_commit_pending)
                geometry_committed <= 1'b1;

            if (flush_level) begin
                if (flush_count != 8'hff)
                    flush_count <= flush_count + 1'b1;
                if (flush_count >= 8'd128) begin
                    flush_level <= 0;
                    flush_count <= 0;
                end
            end

            if (clear_request && !active && !flush_level) begin
                error_flags <= 0;
                error_reason <= 0;
                camera_reset_event_reg <= 1'b0;
            end

            if (capture_request && ready) begin
                accepted <= 1'b1;
                active <= 1'b1;
                profile_id <= next_profile_id;
                epoch <= next_epoch;
                next_profile_id <= next_profile_id + 1'b1;
                angle_latched <= 0;
                timestamp_latched <= timestamp_counter;
                request_toggle <= ~request_toggle;
                snap_feature_mode <= FEATURE_DMA_ENABLE ?
                    ((cfg_feature_mode == 6 && MULTI_CANDIDATE_ENABLE) ? 3'd6 :
                    (((cfg_feature_mode == 2 || cfg_feature_mode == 3 || cfg_feature_mode == 4 || cfg_feature_mode == 5) && COMPACT_POLICY_ENABLE) ? cfg_feature_mode :
                     (cfg_feature_mode == 1 ? 3'd1 : 3'd0))) : 3'd0;
                snap_threshold <= cfg_threshold;
                snap_peak <= cfg_min_peak;
                snap_sum <= cfg_min_sum;
                snap_x0 <= cfg_roi_x0; snap_x1 <= cfg_roi_x1;
                snap_y0 <= cfg_roi_y0; snap_y1 <= cfg_roi_y1;
                snap_minw <= cfg_min_width; snap_maxw <= cfg_max_width;
                raw_done_seen <= 0;
                profile_done_seen <= PROFILE_ENABLE ? 0 : 1;
                profile_frame_seen <= 0;
                watchdog <= 0;
                geometry_committed <= 1'b0;
                error_flags <= 0;
                error_reason <= 0;
            end else if (active) begin
                if (stop_request) begin
                    active <= 0; cancelled <= 1; flush_level <= 1; flush_count <= 0;
                    geometry_committed <= 1'b0;
                    error_reason <= 8'd6;
                    error_flags[ERR_STOP] <= 1'b1;
                    camera_reset_event_reg <= 1'b1;
                    next_epoch <= next_epoch + 1'b1;
                end else if (cam_fault_sync2 ||
                             cam_fault_flags_sync2[ERR_RAW_FIFO] ||
                             (cam_fault_flags_sync2[ERR_ROW_FIFO] && PROFILE_ENABLE) ||
                             profile_error || protocol_fault_event) begin
                    active <= 0; cancelled <= 1; flush_level <= 1; flush_count <= 0;
                    geometry_committed <= 1'b0;
                    error_reason <= cam_fault_sync2 ? 8'd2 : 8'd3;
                    camera_reset_event_reg <= 1'b1;
                    // The fault level is synchronized independently from its
                    // held flag mailbox.  At the first cycle in which the
                    // level is visible, the third settling stage can still
                    // contain the previous mailbox value.  Preserve the
                    // already stable second-stage flags here and explicitly
                    // classify a camera-domain fault so an aborted frame
                    // cannot be reported with an empty ERROR_FLAGS word.
                    error_flags <= error_flags | cam_fault_flags_sync2 |
                                   cam_fault_flags_sync3;
                    if (cam_fault_sync2)
                        error_flags[ERR_CAMERA] <= 1'b1;
                    if (cam_fault_flags_sync2[ERR_LENGTH])
                        error_flags[ERR_LENGTH] <= 1'b1;
                    if (cam_fault_flags_sync2[ERR_RAW_FIFO])
                        error_flags[ERR_RAW_FIFO] <= 1'b1;
                    if (cam_fault_flags_sync2[ERR_ROW_FIFO])
                        error_flags[ERR_ROW_FIFO] <= 1'b1;
                    if (profile_error) error_flags[ERR_PACKET] <= 1'b1;
                    if (protocol_fault_event) error_flags[ERR_PROTOCOL] <= 1'b1;
                    next_epoch <= next_epoch + 1'b1;
                end else if (watchdog >= ((cfg_timeout_cycles == 0) ?
                                           (TIMEOUT_CYCLES-1) :
                                           (cfg_timeout_cycles-1))) begin
                    active <= 0; cancelled <= 1; flush_level <= 1; flush_count <= 0;
                    geometry_committed <= 1'b0;
                    error_reason <= 8'd1;
                    error_flags[ERR_TIMEOUT] <= 1'b1;
                    camera_reset_event_reg <= 1'b1;
                    next_epoch <= next_epoch + 1'b1;
                end else if (raw_done_seen && profile_done_seen &&
                             geometry_committed) begin
                    active <= 0; complete <= 1;
                end else begin
                    watchdog <= watchdog + 1'b1;
                end
            end

            if (m_axis_raw_tvalid && m_axis_raw_tready && m_axis_raw_tlast)
                raw_done_seen <= 1'b1;
            if (profile_done_pulse)
                profile_done_seen <= 1'b1;
        end
    end

    assign pclk_counter = 32'd0;
    assign last_line_pixels = cam_line_pixels_sync2;
    assign last_frame_lines = cam_frame_lines_sync2;
    assign frame_count = {16'd0,gray16_to_bin(cam_frame_count_sync2)};
    // Row count is a frame-local diagnostic.  Export the committed EOF
    // geometry snapshot instead of crossing a counter that is reset at every
    // frame start (which would violate the one-bit Gray transition rule).
    assign last_row_count = cam_frame_lines_sync2[15:0];

endmodule
