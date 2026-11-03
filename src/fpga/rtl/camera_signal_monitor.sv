// Passive MT9V034 FV/LV and grayscale-input monitor.
//
// The system clock owns commands, timeout, result sequencing, and the AXI
// visible snapshot. Camera-side counters run only from cam_pclk. Commands
// cross as a stable-data mailbox qualified by a synchronized toggle. Results
// cross back as a held-data mailbox qualified by done_toggle; the camera
// payload is copied into system-clock registers only after the toggle has
// passed through two synchronizer stages.
module camera_signal_monitor #(
    parameter integer USE_XPM = 0,
    parameter integer USE_SHARED_CAM_RESET = 0,
    parameter integer FRAME_WIDTH = 752,
    parameter integer FRAME_HEIGHT = 480,
    parameter integer SYS_CLOCK_HZ = 100000000,
    parameter integer TIMEOUT_CYCLES = SYS_CLOCK_HZ * 5,
    parameter integer TARGET_FRAMES = 16
) (
    input  wire system_clk,
    input  wire system_resetn,
    input  wire cam_pclk,
    input  wire shared_cam_reset,
    input  wire cam_fv,
    input  wire cam_lv,
    input  wire [7:0] cam_data,
    input  wire start_pulse,
    input  wire abort_pulse,
    output reg  busy,
    output reg  done,
    output reg  snapshot_valid,
    output reg  command_pending,
    output reg  [31:0] result_id,
    output reg  [31:0] error_flags,
    output reg  [31:0] fv_rises,
    output reg  [31:0] fv_falls,
    output reg  [31:0] lv_rises,
    output reg  [31:0] lv_falls,
    output reg  [31:0] fv_high_cycles,
    output reg  [31:0] fv_low_cycles,
    output reg  [31:0] lv_high_cycles,
    output reg  [31:0] lv_low_cycles,
    output reg  [31:0] line_pixels_min,
    output reg  [31:0] line_pixels_max,
    output reg  [31:0] frame_rows_min,
    output reg  [31:0] frame_rows_max,
    output reg  [31:0] complete_frames,
    output reg  [31:0] frame_period_min_pclk,
    output reg  [31:0] frame_period_max_pclk,
    output reg  [31:0] pixel_min,
    output reg  [31:0] pixel_max,
    output reg  [31:0] data_bit_variation_mask
);

    localparam [31:0] ERR_SHORT_LINE       = 32'h00000001;
    localparam [31:0] ERR_BAD_FRAME        = 32'h00000002;
    localparam [31:0] ERR_COUNTER_OVERFLOW = 32'h00000004;
    localparam [31:0] ERR_POLARITY_SUSPECT = 32'h00000008;
    localparam [31:0] ERR_ABORTED          = 32'h00000010;
    localparam [31:0] ERR_TIMEOUT          = 32'h00000020;
    localparam [31:0] ERR_COMMAND_REJECTED = 32'h80000000;

    // threshold: production wrapper shares the core global-only reset with the
    // metadata mailbox. Standalone tests retain an independent local option.
    wire cam_domain_resetn;
    generate
        if (USE_SHARED_CAM_RESET) begin : g_shared_reset
            assign cam_domain_resetn = !shared_cam_reset;
        end else begin : g_local_reset
            wire reset_source = !system_resetn;
            wire local_reset;
            if (USE_XPM) begin : g_xpm
                xpm_cdc_async_rst #(
                    .DEST_SYNC_FF(2), .INIT_SYNC_FF(1), .RST_ACTIVE_HIGH(1)
                ) u_reset (
                    .src_arst(reset_source), .dest_clk(cam_pclk),
                    .dest_arst(local_reset)
                );
            end else begin : g_model
                (* ASYNC_REG = "TRUE" *) reg [1:0] reset_pipe = 2'b11;
                always @(posedge cam_pclk or posedge reset_source)
                    if (reset_source) reset_pipe <= 2'b11;
                    else reset_pipe <= {reset_pipe[0], 1'b0};
                assign local_reset = reset_pipe[1];
            end
            assign cam_domain_resetn = !local_reset;
        end
    endgenerate

    // System-to-camera mailbox. Payload fields remain stable from the command
    // toggle until a later command. Host commands are serialized by busy.
    reg cmd_toggle;
    reg cmd_is_start;
    reg [31:0] cmd_result_id;
    (* ASYNC_REG = "TRUE" *) reg cmd_sync1;
    (* ASYNC_REG = "TRUE" *) reg cmd_sync2;
    reg cmd_seen;
    reg cmd_ack_toggle_cam;
    (* ASYNC_REG = "TRUE" *) reg cmd_ack_sync1;
    (* ASYNC_REG = "TRUE" *) reg cmd_ack_sync2;
    reg cmd_event_pending_cam;
    reg cmd_start_stage_cam;
    reg [31:0] cmd_result_id_stage_cam;

    // Camera-to-system mailbox toggle and held snapshot payload.
    reg done_toggle_cam;
    reg [31:0] done_result_id_cam;
    reg [31:0] done_error_flags_cam;
    reg [31:0] done_fv_rises_cam, done_fv_falls_cam;
    reg [31:0] done_lv_rises_cam, done_lv_falls_cam;
    reg [31:0] done_fv_high_cycles_cam, done_fv_low_cycles_cam;
    reg [31:0] done_lv_high_cycles_cam, done_lv_low_cycles_cam;
    reg [31:0] done_line_min_cam, done_line_max_cam;
    reg [31:0] done_rows_min_cam, done_rows_max_cam;
    reg [31:0] done_frames_cam;
    reg [31:0] done_period_min_cam, done_period_max_cam;
    reg [31:0] done_pixel_min_cam, done_pixel_max_cam;
    reg [31:0] done_data_variation_cam;
    (* ASYNC_REG = "TRUE" *) reg done_sync1;
    (* ASYNC_REG = "TRUE" *) reg done_sync2;
    reg done_seen_sys;
    // The completion payload is a bundled-data mailbox. Stage the complete
    // payload into the receiving clock domain only after the synchronized
    // toggle arrives. The staged ID is compared on a later cycle, and the
    // staged snapshot is published one cycle after that. Raw asynchronous
    // mailbox buses therefore never drive destination-domain control/CE.
    reg [31:0] done_result_id_stage_sys;
    reg [31:0] done_error_flags_stage_sys;
    reg [31:0] done_fv_rises_stage_sys, done_fv_falls_stage_sys;
    reg [31:0] done_lv_rises_stage_sys, done_lv_falls_stage_sys;
    reg [31:0] done_fv_high_cycles_stage_sys, done_fv_low_cycles_stage_sys;
    reg [31:0] done_lv_high_cycles_stage_sys, done_lv_low_cycles_stage_sys;
    reg [31:0] done_line_min_stage_sys, done_line_max_stage_sys;
    reg [31:0] done_rows_min_stage_sys, done_rows_max_stage_sys;
    reg [31:0] done_frames_stage_sys;
    reg [31:0] done_period_min_stage_sys, done_period_max_stage_sys;
    reg [31:0] done_pixel_min_stage_sys, done_pixel_max_stage_sys;
    reg [31:0] done_data_variation_stage_sys;
    reg done_event_pending_sys;
    reg done_match_pending_sys;

    reg [31:0] timeout_count;
    reg [31:0] active_result_id;
    reg cancel_after_ack;

    // Camera-domain working state and accumulators. All working values are
    // reset on a new monitor command; published snapshot registers above are
    // deliberately held until the next successful 16-frame result.
    reg monitor_running;
    reg capture_window;
    reg finish_pending;
    reg fv_previous;
    reg lv_previous;
    reg [31:0] run_result_id_cam;
    reg [31:0] cam_cycle_count;
    reg [31:0] previous_frame_start_cycle;
    reg [31:0] line_pixel_count;
    reg line_open;
    reg [31:0] current_frame_rows;
    reg [31:0] frame_count_work;
    reg line_seen;
    reg frame_seen;
    reg period_seen;
    reg pixel_seen;
    reg [31:0] work_error_flags;
    reg [31:0] work_fv_rises, work_fv_falls;
    reg [31:0] work_lv_rises, work_lv_falls;
    reg [31:0] work_fv_high_cycles, work_fv_low_cycles;
    reg [31:0] work_lv_high_cycles, work_lv_low_cycles;
    reg [31:0] work_line_min, work_line_max;
    reg [31:0] work_rows_min, work_rows_max;
    reg [31:0] work_period_min, work_period_max;
    reg [31:0] work_pixel_min, work_pixel_max;
    reg [7:0] previous_pixel;
    reg [7:0] work_data_variation;

    wire fv_rise = cam_fv && !fv_previous;
    wire fv_fall = !cam_fv && fv_previous;
    wire lv_rise = cam_lv && !lv_previous;
    wire lv_fall = !cam_lv && lv_previous;
    wire line_end_event = line_open && (lv_fall || (fv_fall && lv_previous));
    wire [31:0] rows_at_frame_end = current_frame_rows +
        ((lv_rise && cam_fv) ? 32'd1 : 32'd0);
    wire [31:0] camera_event_errors =
        ((line_end_event && (line_pixel_count != FRAME_WIDTH)) ? ERR_SHORT_LINE : 32'd0) |
        ((fv_fall && (rows_at_frame_end != FRAME_HEIGHT)) ? ERR_BAD_FRAME : 32'd0) |
        (((cam_cycle_count == 32'hffffffff) ||
          ((cam_fv && cam_lv && line_open && !lv_rise) &&
           (line_pixel_count == 32'hffffffff)) ||
          ((lv_rise && cam_fv) && (current_frame_rows == 32'hffffffff)))
            ? ERR_COUNTER_OVERFLOW : 32'd0);

    // 5 s watchdog at the specified 100 MHz system clock. TIMEOUT_CYCLES is
    // parameterized so the RTL testbench can exercise the paused-clock path.
    always @(posedge system_clk or negedge system_resetn) begin
        if (!system_resetn) begin
            busy <= 1'b0;
            done <= 1'b0;
            snapshot_valid <= 1'b0;
            command_pending <= 1'b0;
            result_id <= 32'd0;
            error_flags <= 32'd0;
            fv_rises <= 32'd0; fv_falls <= 32'd0;
            lv_rises <= 32'd0; lv_falls <= 32'd0;
            fv_high_cycles <= 32'd0; fv_low_cycles <= 32'd0;
            lv_high_cycles <= 32'd0; lv_low_cycles <= 32'd0;
            line_pixels_min <= 32'd0; line_pixels_max <= 32'd0;
            frame_rows_min <= 32'd0; frame_rows_max <= 32'd0;
            complete_frames <= 32'd0;
            frame_period_min_pclk <= 32'd0; frame_period_max_pclk <= 32'd0;
            pixel_min <= 32'd0; pixel_max <= 32'd0;
            data_bit_variation_mask <= 32'd0;
            cmd_toggle <= 1'b0;
            cmd_is_start <= 1'b0;
            cmd_result_id <= 32'd0;
            timeout_count <= 32'd0;
            active_result_id <= 32'd0;
            cancel_after_ack <= 1'b0;
            cmd_ack_sync1 <= 1'b0; cmd_ack_sync2 <= 1'b0;
            done_sync1 <= 1'b0; done_sync2 <= 1'b0;
            done_seen_sys <= 1'b0;
            done_result_id_stage_sys <= 32'd0;
            done_error_flags_stage_sys <= 32'd0;
            done_fv_rises_stage_sys <= 32'd0; done_fv_falls_stage_sys <= 32'd0;
            done_lv_rises_stage_sys <= 32'd0; done_lv_falls_stage_sys <= 32'd0;
            done_fv_high_cycles_stage_sys <= 32'd0; done_fv_low_cycles_stage_sys <= 32'd0;
            done_lv_high_cycles_stage_sys <= 32'd0; done_lv_low_cycles_stage_sys <= 32'd0;
            done_line_min_stage_sys <= 32'd0; done_line_max_stage_sys <= 32'd0;
            done_rows_min_stage_sys <= 32'd0; done_rows_max_stage_sys <= 32'd0;
            done_frames_stage_sys <= 32'd0;
            done_period_min_stage_sys <= 32'd0; done_period_max_stage_sys <= 32'd0;
            done_pixel_min_stage_sys <= 32'd0; done_pixel_max_stage_sys <= 32'd0;
            done_data_variation_stage_sys <= 32'd0;
            done_event_pending_sys <= 1'b0;
            done_match_pending_sys <= 1'b0;
        end else begin
            done_sync1 <= done_toggle_cam;
            done_sync2 <= done_sync1;
            cmd_ack_sync1 <= cmd_ack_toggle_cam;
            cmd_ack_sync2 <= cmd_ack_sync1;
            // Keep the toggle mailbox unavailable until the camera domain
            // acknowledges the latest command. If a START times out before
            // the camera clock resumes, send its ABORT only after START is
            // acknowledged; otherwise two toggles could cancel by parity.
            if (command_pending && (cmd_ack_sync2 == cmd_toggle)) begin
                command_pending <= 1'b0;
                if (cancel_after_ack) begin
                    cmd_is_start <= 1'b0;
                    cmd_result_id <= active_result_id;
                    cmd_toggle <= !cmd_toggle;
                    command_pending <= 1'b1;
                    cancel_after_ack <= 1'b0;
                end
            end
            if (start_pulse) begin
                if (!busy && !command_pending && !abort_pulse) begin
                    active_result_id <= result_id + 1'b1;
                    result_id <= result_id + 1'b1;
                    cmd_result_id <= result_id + 1'b1;
                    cmd_is_start <= 1'b1;
                    cmd_toggle <= !cmd_toggle;
                    command_pending <= 1'b1;
                    busy <= 1'b1;
                    done <= 1'b0;
                    snapshot_valid <= 1'b0;
                    error_flags <= 32'd0;
                    timeout_count <= 32'd0;
                    done_event_pending_sys <= 1'b0;
                    done_match_pending_sys <= 1'b0;
                end else begin
                    error_flags <= error_flags | ERR_COMMAND_REJECTED;
                end
            end else if (abort_pulse && busy) begin
                if (command_pending && (cmd_ack_sync2 != cmd_toggle)) begin
                    cancel_after_ack <= 1'b1;
                end else begin
                    cmd_is_start <= 1'b0;
                    cmd_result_id <= active_result_id;
                    cmd_toggle <= !cmd_toggle;
                    command_pending <= 1'b1;
                    cancel_after_ack <= 1'b0;
                end
                busy <= 1'b0;
                done <= 1'b1;
                snapshot_valid <= 1'b0;
                error_flags <= error_flags | ERR_ABORTED;
                timeout_count <= 32'd0;
                done_event_pending_sys <= 1'b0;
                done_match_pending_sys <= 1'b0;
            end else if (done_match_pending_sys) begin
                // One full system-clock cycle after the ID comparison, commit
                // the locally staged snapshot atomically. The camera mailbox
                // is unchanged until a later completed monitor request.
                done_match_pending_sys <= 1'b0;
                if (busy) begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    snapshot_valid <= 1'b1;
                    error_flags <= done_error_flags_stage_sys;
                    fv_rises <= done_fv_rises_stage_sys;
                    fv_falls <= done_fv_falls_stage_sys;
                    lv_rises <= done_lv_rises_stage_sys;
                    lv_falls <= done_lv_falls_stage_sys;
                    fv_high_cycles <= done_fv_high_cycles_stage_sys;
                    fv_low_cycles <= done_fv_low_cycles_stage_sys;
                    lv_high_cycles <= done_lv_high_cycles_stage_sys;
                    lv_low_cycles <= done_lv_low_cycles_stage_sys;
                    line_pixels_min <= done_line_min_stage_sys;
                    line_pixels_max <= done_line_max_stage_sys;
                    frame_rows_min <= done_rows_min_stage_sys;
                    frame_rows_max <= done_rows_max_stage_sys;
                    complete_frames <= done_frames_stage_sys;
                    frame_period_min_pclk <= done_period_min_stage_sys;
                    frame_period_max_pclk <= done_period_max_stage_sys;
                    pixel_min <= done_pixel_min_stage_sys;
                    pixel_max <= done_pixel_max_stage_sys;
                    data_bit_variation_mask <= {24'd0, done_data_variation_stage_sys[7:0]};
                    timeout_count <= 32'd0;
                end
            end else if (done_event_pending_sys) begin
                done_event_pending_sys <= 1'b0;
                done_match_pending_sys <= busy &&
                    (done_result_id_stage_sys == active_result_id);
            end else if (done_sync2 != done_seen_sys) begin
                // The two-flop toggle synchronizer says the camera's held
                // mailbox is ready. Register the whole coherent payload
                // locally, then perform the 32-bit stale-ID compare in a
                // separate system-clock stage. While these CDC pipeline
                // cycles run, pause the timeout
                // counter so a just-completed monitor cannot lose a race at
                // the watchdog boundary.
                done_seen_sys <= done_sync2;
                done_result_id_stage_sys <= done_result_id_cam;
                done_error_flags_stage_sys <= done_error_flags_cam;
                done_fv_rises_stage_sys <= done_fv_rises_cam;
                done_fv_falls_stage_sys <= done_fv_falls_cam;
                done_lv_rises_stage_sys <= done_lv_rises_cam;
                done_lv_falls_stage_sys <= done_lv_falls_cam;
                done_fv_high_cycles_stage_sys <= done_fv_high_cycles_cam;
                done_fv_low_cycles_stage_sys <= done_fv_low_cycles_cam;
                done_lv_high_cycles_stage_sys <= done_lv_high_cycles_cam;
                done_lv_low_cycles_stage_sys <= done_lv_low_cycles_cam;
                done_line_min_stage_sys <= done_line_min_cam;
                done_line_max_stage_sys <= done_line_max_cam;
                done_rows_min_stage_sys <= done_rows_min_cam;
                done_rows_max_stage_sys <= done_rows_max_cam;
                done_frames_stage_sys <= done_frames_cam;
                done_period_min_stage_sys <= done_period_min_cam;
                done_period_max_stage_sys <= done_period_max_cam;
                done_pixel_min_stage_sys <= done_pixel_min_cam;
                done_pixel_max_stage_sys <= done_pixel_max_cam;
                done_data_variation_stage_sys <= done_data_variation_cam;
                done_event_pending_sys <= 1'b1;
                timeout_count <= timeout_count;
            end else if (busy) begin
                if ((TIMEOUT_CYCLES == 0) ||
                    (timeout_count >= TIMEOUT_CYCLES - 1)) begin
                    if (command_pending && (cmd_ack_sync2 != cmd_toggle)) begin
                        cancel_after_ack <= 1'b1;
                    end else begin
                        cmd_is_start <= 1'b0;
                        cmd_result_id <= active_result_id;
                        cmd_toggle <= !cmd_toggle;
                        command_pending <= 1'b1;
                        cancel_after_ack <= 1'b0;
                    end
                    busy <= 1'b0;
                    done <= 1'b1;
                    snapshot_valid <= 1'b0;
                    error_flags <= error_flags | ERR_TIMEOUT;
                    timeout_count <= 32'd0;
                end else begin
                    timeout_count <= timeout_count + 1'b1;
                end
            end

            if (abort_pulse && !busy) begin
                // Idle abort is harmless and does not disturb a published
                // snapshot or create a camera-domain command.
                error_flags <= error_flags;
            end
        end
    end

    // Camera-domain capture engine. FV and LV are interpreted high-active;
    // raw high/low cycle and edge counts are retained so a polarity mismatch
    // is diagnosable rather than silently auto-corrected.
    always @(posedge cam_pclk or negedge cam_domain_resetn) begin
        if (!cam_domain_resetn) begin
            cmd_sync1 <= 1'b0;
            cmd_sync2 <= 1'b0;
            cmd_seen <= 1'b0;
            cmd_ack_toggle_cam <= 1'b0;
            cmd_event_pending_cam <= 1'b0;
            cmd_start_stage_cam <= 1'b0;
            cmd_result_id_stage_cam <= 32'd0;
            done_toggle_cam <= 1'b0;
            done_result_id_cam <= 32'd0;
            done_error_flags_cam <= 32'd0;
            done_fv_rises_cam <= 32'd0; done_fv_falls_cam <= 32'd0;
            done_lv_rises_cam <= 32'd0; done_lv_falls_cam <= 32'd0;
            done_fv_high_cycles_cam <= 32'd0; done_fv_low_cycles_cam <= 32'd0;
            done_lv_high_cycles_cam <= 32'd0; done_lv_low_cycles_cam <= 32'd0;
            done_line_min_cam <= 32'd0; done_line_max_cam <= 32'd0;
            done_rows_min_cam <= 32'd0; done_rows_max_cam <= 32'd0;
            done_frames_cam <= 32'd0;
            done_period_min_cam <= 32'd0; done_period_max_cam <= 32'd0;
            done_pixel_min_cam <= 32'd0; done_pixel_max_cam <= 32'd0;
            done_data_variation_cam <= 32'd0;
            monitor_running <= 1'b0;
            capture_window <= 1'b0;
            finish_pending <= 1'b0;
            fv_previous <= 1'b0;
            lv_previous <= 1'b0;
            run_result_id_cam <= 32'd0;
            cam_cycle_count <= 32'd0;
            previous_frame_start_cycle <= 32'd0;
            line_pixel_count <= 32'd0;
            line_open <= 1'b0;
            current_frame_rows <= 32'd0;
            frame_count_work <= 32'd0;
            line_seen <= 1'b0; frame_seen <= 1'b0;
            period_seen <= 1'b0; pixel_seen <= 1'b0;
            work_error_flags <= 32'd0;
            work_fv_rises <= 32'd0; work_fv_falls <= 32'd0;
            work_lv_rises <= 32'd0; work_lv_falls <= 32'd0;
            work_fv_high_cycles <= 32'd0; work_fv_low_cycles <= 32'd0;
            work_lv_high_cycles <= 32'd0; work_lv_low_cycles <= 32'd0;
            work_line_min <= 32'hffffffff; work_line_max <= 32'd0;
            work_rows_min <= 32'hffffffff; work_rows_max <= 32'd0;
            work_period_min <= 32'hffffffff; work_period_max <= 32'd0;
            work_pixel_min <= 32'hffffffff; work_pixel_max <= 32'd0;
            previous_pixel <= 8'd0;
            work_data_variation <= 8'd0;
        end else begin
            cmd_sync1 <= cmd_toggle;
            cmd_sync2 <= cmd_sync1;

            if (cmd_event_pending_cam) begin
                // Command fields were captured locally after the synchronized
                // toggle. Acknowledge only after the staged payload has been
                // consumed, so the system source holds it stable throughout.
                cmd_event_pending_cam <= 1'b0;
                cmd_ack_toggle_cam <= cmd_seen;
                fv_previous <= cam_fv;
                lv_previous <= cam_lv;
                finish_pending <= 1'b0;
                if (cmd_start_stage_cam) begin
                    monitor_running <= 1'b1;
                    capture_window <= 1'b0;
                    run_result_id_cam <= cmd_result_id_stage_cam;
                    cam_cycle_count <= 32'd0;
                    previous_frame_start_cycle <= 32'd0;
                    line_pixel_count <= 32'd0;
                    line_open <= 1'b0;
                    current_frame_rows <= 32'd0;
                    frame_count_work <= 32'd0;
                    line_seen <= 1'b0; frame_seen <= 1'b0;
                    period_seen <= 1'b0; pixel_seen <= 1'b0;
                    work_error_flags <= 32'd0;
                    work_fv_rises <= 32'd0; work_fv_falls <= 32'd0;
                    work_lv_rises <= 32'd0; work_lv_falls <= 32'd0;
                    work_fv_high_cycles <= 32'd0; work_fv_low_cycles <= 32'd0;
                    work_line_min <= 32'hffffffff; work_line_max <= 32'd0;
                    work_rows_min <= 32'hffffffff; work_rows_max <= 32'd0;
                    work_period_min <= 32'hffffffff; work_period_max <= 32'd0;
                    work_pixel_min <= 32'hffffffff; work_pixel_max <= 32'd0;
                    previous_pixel <= 8'd0;
                    work_data_variation <= 8'd0;
                end else begin
                    monitor_running <= 1'b0;
                    capture_window <= 1'b0;
                    finish_pending <= 1'b0;
                    line_open <= 1'b0;
                end
            end else if (cmd_sync2 != cmd_seen) begin
                // The payload is stable until ack returns through the system
                // synchronizer. Capture it in the camera domain first; only
                // the synchronized toggle drives receiver-domain control.
                cmd_seen <= cmd_sync2;
                cmd_start_stage_cam <= cmd_is_start;
                cmd_result_id_stage_cam <= cmd_result_id;
                cmd_event_pending_cam <= 1'b1;
            end else if (finish_pending) begin
                // Publish one cycle after the final falling FV edge so all
                // nonblocking working-counter updates from that edge settle.
                monitor_running <= 1'b0;
                capture_window <= 1'b0;
                finish_pending <= 1'b0;
                done_result_id_cam <= run_result_id_cam;
                done_error_flags_cam <= work_error_flags |
                    ((work_fv_high_cycles < work_fv_low_cycles) ? ERR_POLARITY_SUSPECT : 32'd0);
                done_fv_rises_cam <= work_fv_rises;
                done_fv_falls_cam <= work_fv_falls;
                done_lv_rises_cam <= work_lv_rises;
                done_lv_falls_cam <= work_lv_falls;
                done_fv_high_cycles_cam <= work_fv_high_cycles;
                done_fv_low_cycles_cam <= work_fv_low_cycles;
                done_lv_high_cycles_cam <= work_lv_high_cycles;
                done_lv_low_cycles_cam <= work_lv_low_cycles;
                done_line_min_cam <= line_seen ? work_line_min : 32'd0;
                done_line_max_cam <= work_line_max;
                done_rows_min_cam <= frame_seen ? work_rows_min : 32'd0;
                done_rows_max_cam <= work_rows_max;
                done_frames_cam <= frame_count_work;
                done_period_min_cam <= period_seen ? work_period_min : 32'd0;
                done_period_max_cam <= work_period_max;
                done_pixel_min_cam <= pixel_seen ? work_pixel_min : 32'd0;
                done_pixel_max_cam <= work_pixel_max;
                done_data_variation_cam <= {24'd0, work_data_variation};
                done_toggle_cam <= !done_toggle_cam;
            end else begin
                fv_previous <= cam_fv;
                lv_previous <= cam_lv;
                if (monitor_running) begin
                if (capture_window || fv_rise) begin
                        // Count raw levels and transitions for the entire
                        // observation from first FV rise through final fall.
                        if (cam_fv) work_fv_high_cycles <= work_fv_high_cycles + 1'b1;
                        else work_fv_low_cycles <= work_fv_low_cycles + 1'b1;
                        if (cam_lv) work_lv_high_cycles <= work_lv_high_cycles + 1'b1;
                        else work_lv_low_cycles <= work_lv_low_cycles + 1'b1;
                        if (fv_rise) work_fv_rises <= work_fv_rises + 1'b1;
                        if (fv_fall) work_fv_falls <= work_fv_falls + 1'b1;
                        if (lv_rise) work_lv_rises <= work_lv_rises + 1'b1;
                        if (lv_fall) work_lv_falls <= work_lv_falls + 1'b1;

                        if (fv_rise && !capture_window) begin
                            capture_window <= 1'b1;
                            cam_cycle_count <= 32'd1;
                            previous_frame_start_cycle <= 32'd0;
                            current_frame_rows <= lv_rise ? 32'd1 : 32'd0;
                            line_pixel_count <= (lv_rise && cam_lv) ? 32'd1 : 32'd0;
                            line_open <= lv_rise && cam_lv;
                        end else begin
                            if (cam_cycle_count != 32'hffffffff)
                                cam_cycle_count <= cam_cycle_count + 1'b1;
                            if (fv_rise) begin
                                if (cam_cycle_count >= previous_frame_start_cycle) begin
                                    if (!period_seen ||
                                        ((cam_cycle_count - previous_frame_start_cycle) < work_period_min))
                                        work_period_min <= cam_cycle_count - previous_frame_start_cycle;
                                    if (!period_seen ||
                                        ((cam_cycle_count - previous_frame_start_cycle) > work_period_max))
                                        work_period_max <= cam_cycle_count - previous_frame_start_cycle;
                                    period_seen <= 1'b1;
                                    end
                                previous_frame_start_cycle <= cam_cycle_count;
                                current_frame_rows <= lv_rise ? 32'd1 : 32'd0;
                                line_pixel_count <= (lv_rise && cam_lv) ? 32'd1 : 32'd0;
                                line_open <= lv_rise && cam_lv;
                            end else begin
                                if (lv_rise && cam_fv) begin
                                    current_frame_rows <= current_frame_rows + 1'b1;
                                    line_pixel_count <= 32'd1;
                                    line_open <= 1'b1;
                                end else if (cam_fv && cam_lv && line_open) begin
                                    if (line_pixel_count != 32'hffffffff)
                                        line_pixel_count <= line_pixel_count + 1'b1;
                                end
                            end
                        end

                        // Pixel value statistics cover high-active FV and LV.
                        if (cam_fv && cam_lv) begin
                            if (!pixel_seen || cam_data < work_pixel_min) work_pixel_min <= cam_data;
                            if (!pixel_seen || cam_data > work_pixel_max) work_pixel_max <= cam_data;
                            if (pixel_seen) work_data_variation <= work_data_variation |
                                (cam_data ^ previous_pixel);
                            previous_pixel <= cam_data;
                            pixel_seen <= 1'b1;
                        end

                        // A line ends on LV falling, or at FV falling if LV
                        // was malformed and remained asserted.
                        if (line_open && (lv_fall || (fv_fall && lv_previous))) begin
                            if (!line_seen || line_pixel_count < work_line_min)
                                work_line_min <= line_pixel_count;
                            if (!line_seen || line_pixel_count > work_line_max)
                                work_line_max <= line_pixel_count;
                            line_seen <= 1'b1;
                            line_open <= 1'b0;
                        end

                        if (fv_fall) begin
                            if (!frame_seen || rows_at_frame_end < work_rows_min)
                                work_rows_min <= rows_at_frame_end;
                            if (!frame_seen || rows_at_frame_end > work_rows_max)
                                work_rows_max <= rows_at_frame_end;
                            frame_seen <= 1'b1;
                            frame_count_work <= frame_count_work + 1'b1;
                            if (frame_count_work + 1'b1 >= TARGET_FRAMES)
                                finish_pending <= 1'b1;
                        end
                        if (camera_event_errors != 0)
                            work_error_flags <= work_error_flags | camera_event_errors;
                    end else if (fv_rise) begin
                        // This branch is unreachable because fv_rise opens
                        // the observation window above, kept for clarity.
                        capture_window <= 1'b1;
                    end
                end
            end
        end
    end
endmodule
