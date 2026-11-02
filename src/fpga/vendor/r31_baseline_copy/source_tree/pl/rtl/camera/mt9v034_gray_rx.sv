`timescale 1ns/1ps

// One-byte-per-pixel MT9V034 DVP receiver.
//
// The receiver deliberately models the camera-domain contract used by P1:
// FV/HREF are explicit, one gray byte is sampled on every active clock, and
// the original frame coordinates are retained.  It does not pack RGB565
// bytes and it does not silently pad malformed lines.
module mt9v034_gray_rx #(
    parameter integer FRAME_WIDTH  = 640,
    parameter integer FRAME_HEIGHT = 480,
    parameter integer FV_ACTIVE    = 1,
    parameter integer LV_ACTIVE    = 1
) (
    input  logic       clk,
    input  logic       rst,
    input  logic       cam_fv,
    input  logic       cam_lv,
    input  logic [7:0] cam_data,

    output logic       pixel_valid,
    output logic [9:0] pixel_x,
    output logic [9:0] pixel_y,
    output logic [7:0] gray,
    output logic       line_end,
    output logic [9:0] line_y,
    output logic       line_bad,
    output logic       frame_start,
    output logic       frame_end,
    output logic [15:0] frame_errors
);

    // Error flag definitions are local to the camera-domain diagnostic
    // record.  The SLP2 packet continues to preserve the low 16 bits of this
    // value without assigning it a new meaning.
    localparam integer ERR_SHORT_LINE = 0;
    localparam integer ERR_LONG_LINE  = 1;
    localparam integer ERR_EXTRA_LINE = 2;
    localparam integer ERR_BAD_SYNC   = 3;
    localparam integer ERR_SHORT_FRAME = 4;

    wire fv_level = FV_ACTIVE ? cam_fv : ~cam_fv;
    wire lv_level = LV_ACTIVE ? cam_lv : ~cam_lv;

    logic prev_fv;
    logic prev_lv;
    logic frame_active;
    logic in_line;
    logic [15:0] pixel_count;
    logic [15:0] line_count;

    // Blocking temporaries keep the event ordering explicit when FV falls at
    // the same edge as LV.  They are not state and are assigned every cycle.
    logic [15:0] line_index_work;
    logic [15:0] column_index_work;
    logic [15:0] error_work;
    logic        line_event_work;
    logic        line_bad_work;
    logic [15:0] lines_after_work;

    always_ff @(posedge clk) begin
        if (rst) begin
            prev_fv      <= 1'b0;
            prev_lv      <= 1'b0;
            frame_active <= 1'b0;
            in_line      <= 1'b0;
            pixel_count  <= 16'd0;
            line_count   <= 16'd0;
            pixel_valid  <= 1'b0;
            pixel_x      <= 10'd0;
            pixel_y      <= 10'd0;
            gray         <= 8'd0;
            line_end     <= 1'b0;
            line_y       <= 10'd0;
            line_bad     <= 1'b0;
            frame_start  <= 1'b0;
            frame_end    <= 1'b0;
            frame_errors <= 16'd0;
        end else begin
            pixel_valid <= 1'b0;
            line_end    <= 1'b0;
            line_bad    <= 1'b0;
            frame_start <= 1'b0;
            frame_end   <= 1'b0;

            // Start with the previous sticky diagnostic value.  A rising FV
            // begins a fresh frame and therefore clears the previous frame's
            // errors before recording any malformed input on this edge.
            error_work = frame_errors;
            if (fv_level && !prev_fv) begin
                frame_start   <= 1'b1;
                frame_active  <= 1'b1;
                pixel_count   <= 16'd0;
                line_count    <= 16'd0;
                in_line       <= 1'b0;
                error_work    = 16'd0;
            end

            line_index_work   = (fv_level && !prev_fv) ? 16'd0 : line_count;
            column_index_work = (fv_level && !prev_fv) ? 16'd0 : pixel_count;
            line_event_work   = 1'b0;
            line_bad_work     = 1'b0;

            // A pixel is accepted only while the frame is active.  Pixels
            // beyond the configured geometry are consumed for diagnostics,
            // but are not exposed with a wrapped coordinate.
            if ((frame_active || (fv_level && !prev_fv)) && fv_level && lv_level) begin
                in_line <= 1'b1;
                if (column_index_work < FRAME_WIDTH && line_index_work < FRAME_HEIGHT) begin
                    pixel_valid <= 1'b1;
                    pixel_x     <= column_index_work[9:0];
                    pixel_y     <= line_index_work[9:0];
                    gray        <= cam_data;
                end
                if (column_index_work < 16'hffff)
                    pixel_count <= column_index_work + 16'd1;
                if (column_index_work >= FRAME_WIDTH)
                    error_work[ERR_LONG_LINE] = 1'b1;
                if (line_index_work >= FRAME_HEIGHT)
                    error_work[ERR_EXTRA_LINE] = 1'b1;
            end

            // LV falling edge ends a line.  If FV falls while LV remains
            // asserted, the FV edge also ends the line and marks it bad.
            if (in_line && ((!lv_level && prev_lv) || (!fv_level && prev_fv))) begin
                line_event_work = 1'b1;
                line_bad_work   = (pixel_count != FRAME_WIDTH) ||
                                   (line_count >= FRAME_HEIGHT) ||
                                   // FV may terminate a line while LV is
                                   // still active.  It is a valid boundary
                                   // event for recovery, but the line must be
                                   // reported malformed rather than silently
                                   // accepted as a full-width row.
                                   (!fv_level && prev_fv && lv_level);
                line_end <= 1'b1;
                line_y   <= line_count[9:0];
                line_bad <= line_bad_work;
                if (pixel_count < FRAME_WIDTH)
                    error_work[ERR_SHORT_LINE] = 1'b1;
                if (pixel_count > FRAME_WIDTH)
                    error_work[ERR_LONG_LINE] = 1'b1;
                if (line_count >= FRAME_HEIGHT)
                    error_work[ERR_EXTRA_LINE] = 1'b1;
                in_line     <= 1'b0;
                pixel_count <= 16'd0;
                if (line_count < 16'hffff)
                    line_count <= line_count + 16'd1;
            end

            // LV active outside FV is malformed synchronization.  This is
            // intentionally sticky so a short diagnostic record can explain
            // why a subsequent row or frame was rejected.
            if (lv_level && !fv_level)
                error_work[ERR_BAD_SYNC] = 1'b1;

            if (!fv_level && prev_fv && frame_active) begin
                frame_end   <= 1'b1;
                frame_active <= 1'b0;
                if (lv_level)
                    error_work[ERR_BAD_SYNC] = 1'b1;
                // Account for a line that ends on the same FV falling edge.
                lines_after_work = line_count + (line_event_work ? 16'd1 : 16'd0);
                if (lines_after_work != FRAME_HEIGHT)
                    error_work[ERR_SHORT_FRAME] = 1'b1;
            end

            // FV can remain high until after the final LV falling edge.  The
            // normal path is therefore frame_end without line_end; if FV and
            // LV fall together both output pulses are intentionally asserted.
            frame_errors <= error_work;
            prev_fv      <= fv_level;
            prev_lv      <= lv_level;
        end
    end
endmodule
