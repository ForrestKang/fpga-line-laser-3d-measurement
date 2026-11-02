`timescale 1ns/1ps

// Measures an asynchronous camera clock against the PYNQ-Z2 100 MHz clock.
// A Gray-coded free-running camera-domain counter is synchronized into the
// system domain; the reported count is the number of camera edges per window.
// P2 controlled copy of the asynchronous PCLK meter.
// Source: structured_light_hw_sim/pynq_z2_mt9v034_bringup/rtl/pclk_frequency_meter.sv
// P2 uses a 100 MHz system clock and a 10 ms measurement window.
module pclk_frequency_meter #(
    parameter integer USE_XPM = 0,
    parameter integer SHARED_CAM_REQUEST = 0,
    parameter integer SHARED_CAM_SYSTEM_RESET = 0,
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer WINDOW_CYCLES = SYS_CLK_HZ
) (
    input  wire        sys_clk,
    input  wire        sys_resetn,
    input  wire        cam_pclk,
    input  wire        cam_resetn,
    input  wire        cam_reset_request,
    input  wire        shared_cam_system_reset,
    output reg  [31:0] cam_pclk_hz,
    output reg         measurement_valid,
    output wire [31:0] synchronized_edge_count
);
    reg [31:0] cam_count_bin;
    reg [31:0] cam_count_gray;
    // The wrapper supplies the CAM_CTRL next-state FF output on the same
    // edge as its software-visible register. Standalone users already supply
    // a registered cam_resetn and retain their original assertion latency.
    wire cam_reset_source = SHARED_CAM_REQUEST ? cam_reset_request : !cam_resetn;
    wire system_reset_source = !sys_resetn;
    wire cam_request_reset, cam_system_reset;
    wire cam_reset = cam_request_reset | cam_system_reset;
    generate
        if (USE_XPM) begin : g_reset_xpm
            xpm_cdc_async_rst #(
                .DEST_SYNC_FF(2), .INIT_SYNC_FF(0), .RST_ACTIVE_HIGH(1)
            ) u_reset (
                .src_arst(cam_reset_source), .dest_clk(cam_pclk),
                .dest_arst(cam_request_reset)
            );
            if (SHARED_CAM_SYSTEM_RESET) begin : g_shared_system_reset
                assign cam_system_reset = shared_cam_system_reset;
            end else begin : g_local_system_reset
                xpm_cdc_async_rst #(
                    .DEST_SYNC_FF(2), .INIT_SYNC_FF(0), .RST_ACTIVE_HIGH(1)
                ) u_system_reset (
                    .src_arst(system_reset_source), .dest_clk(cam_pclk),
                    .dest_arst(cam_system_reset)
                );
            end
        end else begin : g_reset_model
            (* ASYNC_REG = "TRUE" *) reg [1:0] reset_pipe = 2'b11;
            (* ASYNC_REG = "TRUE" *) reg [1:0] system_pipe = 2'b11;
            always @(posedge cam_pclk or posedge cam_reset_source)
                if (cam_reset_source) reset_pipe <= 2'b11;
                else reset_pipe <= {reset_pipe[0], 1'b0};
            always @(posedge cam_pclk or posedge system_reset_source)
                if (system_reset_source) system_pipe <= 2'b11;
                else system_pipe <= {system_pipe[0], 1'b0};
            assign cam_request_reset = reset_pipe[1];
            assign cam_system_reset = SHARED_CAM_SYSTEM_RESET ?
                                      shared_cam_system_reset : system_pipe[1];
        end
    endgenerate

    (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg [31:0] gray_sync_1;
    (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *) reg [31:0] gray_sync_2;
    // Decode after the two-stage CDC and register the result.  This separates
    // Gray decoding from the subtraction/rate scaling critical path.  The
    // fixed additional system-clock latency cancels between adjacent windows.
    reg [31:0] binary_edge_count;

    localparam integer WINDOW_WIDTH = (WINDOW_CYCLES <= 2) ? 1 : $clog2(WINDOW_CYCLES);
    // After CAM_CTRL releases the sensor reset, allow the camera-local reset
    // pipe, Gray synchronizer, and binary decode pipeline to settle before
    // counting a measurement window.  The short warmup samples are discarded;
    // this is deliberately in the system domain and does not add another
    // asynchronous reset CDC path.
    localparam integer RESET_WARMUP_CYCLES = 16;
    localparam integer WARMUP_WIDTH =
        (RESET_WARMUP_CYCLES <= 1) ? 1 : $clog2(RESET_WARMUP_CYCLES + 1);
    reg [WARMUP_WIDTH-1:0] reset_warmup_count;
    reg [WINDOW_WIDTH-1:0] window_count;
    reg [31:0] previous_edge_count;
    wire [31:0] edge_delta = synchronized_edge_count - previous_edge_count;
    // The normal 100 MHz / 10 ms configuration has an exact integer scale
    // of 100. Keep that path as a multiply-only expression so the meter
    // cannot infer a 64-bit divider on the timing-critical system clock.
    // Non-integral parameterizations retain the general reference equation.
    localparam integer RATE_SCALE_EXACT =
        (WINDOW_CYCLES > 0 && (SYS_CLK_HZ % WINDOW_CYCLES) == 0) ?
        (SYS_CLK_HZ / WINDOW_CYCLES) : 0;
    wire [63:0] edge_rate_hz;
    generate
        if (RATE_SCALE_EXACT > 0) begin : g_exact_rate
            assign edge_rate_hz = edge_delta * RATE_SCALE_EXACT;
        end else begin : g_general_rate
            wire [63:0] edge_rate_product = edge_delta * SYS_CLK_HZ;
            assign edge_rate_hz = edge_rate_product / WINDOW_CYCLES;
        end
    endgenerate

    function automatic [31:0] gray_to_bin(input [31:0] value);
        integer i;
        begin
            gray_to_bin[31] = value[31];
            for (i = 30; i >= 0; i = i - 1)
                gray_to_bin[i] = gray_to_bin[i+1] ^ value[i];
        end
    endfunction

    assign synchronized_edge_count = binary_edge_count;

    always @(posedge cam_pclk or posedge cam_reset) begin
        if (cam_reset) begin
            cam_count_bin  <= 32'd0;
            cam_count_gray <= 32'd0;
        end else begin
            cam_count_bin  <= cam_count_bin + 1'b1;
            cam_count_gray <= ((cam_count_bin + 1'b1) >> 1) ^ (cam_count_bin + 1'b1);
        end
    end

    always @(posedge sys_clk or negedge sys_resetn) begin
        if (!sys_resetn) begin
            gray_sync_1          <= 32'd0;
            gray_sync_2          <= 32'd0;
            binary_edge_count    <= 32'd0;
            reset_warmup_count   <= RESET_WARMUP_CYCLES;
            window_count         <= {WINDOW_WIDTH{1'b0}};
            previous_edge_count  <= 32'd0;
            cam_pclk_hz          <= 32'd0;
            measurement_valid    <= 1'b0;
        end else begin
            // Keep both Gray synchronizer stages unconditional.  CAM_CTRL
            // must not add a clock enable/data mux to the CDC pipeline.
            gray_sync_1       <= cam_count_gray;
            gray_sync_2       <= gray_sync_1;
            binary_edge_count <= gray_to_bin(gray_sync_2);
            measurement_valid <= 1'b0;

            if (!cam_resetn) begin
                // The source counter is asynchronously cleared by camera
                // reset; the always-running CDC pipeline then settles to
                // zero. Discard the measurement epoch, rather than resetting
                // the synchronizer itself, to prevent a false large delta.
                reset_warmup_count   <= RESET_WARMUP_CYCLES;
                window_count         <= {WINDOW_WIDTH{1'b0}};
                previous_edge_count  <= 32'd0;
                cam_pclk_hz          <= 32'd0;
            end else if (reset_warmup_count != 0) begin
                // Rebaseline continuously while the post-reset pipeline
                // fills.  The warmup samples cannot become a rate result.
                reset_warmup_count  <= reset_warmup_count - 1'b1;
                window_count        <= {WINDOW_WIDTH{1'b0}};
                previous_edge_count <= binary_edge_count;
            end else if (window_count == WINDOW_CYCLES - 1) begin
                window_count        <= {WINDOW_WIDTH{1'b0}};
                // Convert observed edges per measurement window to Hz.
                cam_pclk_hz         <= edge_rate_hz[31:0];
                previous_edge_count <= synchronized_edge_count;
                measurement_valid   <= 1'b1;
            end else begin
                window_count <= window_count + 1'b1;
            end
        end
    end
endmodule
