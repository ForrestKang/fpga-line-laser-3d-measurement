`timescale 1ns/1ps

// FPGA-friendly ROW weighted centroid for an 8-bit grayscale stream.
//
// The input is one pixel per camera clock.  Positive support is accumulated
// in original frame coordinates.  At line_end the current row is handed to a
// 39-iteration unsigned restoring divider while the next row may accumulate.
module row_centroid_gray #(
    parameter integer FRACTIONAL_BITS = 12
) (
    input  logic       clk,
    input  logic       rst,
    input  logic       frame_start,
    input  logic       pixel_valid,
    input  logic [9:0] pixel_x,
    input  logic [9:0] pixel_y,
    input  logic [7:0] gray,
    input  logic       line_end,
    input  logic [9:0] line_y,
    input  logic       line_bad,

    input  logic [7:0] threshold,
    input  logic [9:0] roi_x0,
    input  logic [9:0] roi_x1,
    input  logic [9:0] roi_y0,
    input  logic [9:0] roi_y1,
    input  logic [9:0] min_width,
    input  logic [9:0] max_width,
    input  logic [17:0] min_sum,
    input  logic [7:0] min_peak,

    output logic       row_valid,
    output logic [9:0] row,
    output logic [21:0] xc_fixed,
    output logic [17:0] s0,
    output logic [26:0] s1,
    output logic [7:0]  peak,
    output logic [9:0]  width,
    output logic [9:0]  candidate_count,
    output logic [9:0]  first_support_x,
    output logic [9:0]  last_support_x,
    output logic [9:0]  saturated_support_count,
    output logic [15:0] flags,
    output logic [7:0]  quality,
    output logic        valid,
    output logic [15:0] diagnostics,
    output logic [21:0] xc_all,
    output logic [7:0] roi_raw_peak,
    output logic [9:0] roi_sat250_count,
    output logic [2:0] ridge_flags,
    output logic        busy,
    output logic        overrun
);

    localparam integer FLAG_ZERO_SUM = 0;
    localparam integer FLAG_WEAK     = 1;
    localparam integer FLAG_WIDE     = 2;
    localparam integer FLAG_MULTI    = 3;
    localparam integer FLAG_BAD      = 4;
    localparam integer FLAG_OVERRUN  = 5;
    localparam integer DIAG_NARROW   = 0;
    localparam integer DIAG_ROI      = 1;

    logic [7:0] acc_raw_peak, cap_raw_peak, next_raw_peak;
    logic [9:0] acc_sat250, cap_sat250, next_sat250;
    logic [17:0] acc_s0;
    logic [26:0] acc_s1;
    logic [7:0]  acc_peak;
    logic [9:0]  acc_first_x;
    logic [9:0]  acc_last_x;
    logic [9:0]  acc_candidates;
    logic [9:0]  acc_saturated;
    logic        acc_in_support;
    logic        acc_have_support;
    logic        acc_bad;
    logic        acc_roi_outside;

    logic [17:0] cap_s0;
    logic [26:0] cap_s1;
    logic [7:0]  cap_peak;
    logic [9:0]  cap_width;
    logic [9:0]  cap_candidates;
    logic [9:0]  cap_first_x, cap_last_x, cap_saturated;
    logic [9:0]  cap_row;
    logic        cap_bad;
    logic        cap_roi_outside;
    logic        cap_overrun;

    logic        div_busy;
    logic [5:0]  div_count;
    logic [38:0] div_num;
    logic [17:0] div_den;
    logic [18:0] div_rem;
    logic [38:0] div_quot;
    logic        pending_overrun;

    // Per-cycle arithmetic temporaries.  They are assigned before use in the
    // sequential process, which also makes the final pixel visible to the
    // line-end transaction.
    logic [8:0]  weight_tmp;
    logic [26:0] product_tmp;
    logic [17:0] next_s0;
    logic [26:0] next_s1;
    logic [7:0]  next_peak;
    logic [9:0]  next_first_x;
    logic [9:0]  next_last_x;
    logic [9:0]  next_candidates;
    logic [9:0]  next_saturated;
    logic        next_in_support;
    logic        next_have_support;
    logic        next_bad;
    logic        next_roi_outside;
    logic [18:0] rem_shift_tmp;
    logic [38:0] num_shift_tmp;
    logic [38:0] quot_shift_tmp;
    logic [15:0] flags_tmp;
    logic [15:0] diagnostics_tmp;
    logic        valid_tmp;
    logic        inside_roi_tmp;

    assign busy = div_busy;

    // RPG1 is independent of centroid/classification. History advances only
    // for valid native source pixels; arbitrary clock bubbles are harmless.
    logic [7:0] ridge_hist [0:10];
    logic [3:0] ridge_hist_count, ridge_right_count;
    logic [9:0] ridge_seen_x, ridge_seen_y, ridge_start, ridge_end, ridge_components;
    logic ridge_in_support;
    logic ridge_seen, ridge_bad, ridge_left_complete, ridge_have;
    logic [10:0] ridge_left_sum, ridge_right_sum;
    logic [7:0] ridge_first_gray, ridge_last_gray, ridge_peak;
    logic [2:0] cap_ridge_flags;
    integer ri;
    integer rl, rr, rc, rstart, rend, rfirst, rlast, rpeak, rcomponents;
    logic rcomplete, rshape, rcontrast, rbad, rhave, rleft, rin;
    always_ff @(posedge clk) begin
        if (rst || frame_start) begin
            ridge_hist_count <= 0; ridge_right_count <= 0;
            ridge_start <= 0; ridge_components <= 0; ridge_in_support <= 0;
            ridge_seen <= 0; ridge_bad <= 0; ridge_left_complete <= 0; ridge_have <= 0;
            ridge_left_sum <= 0; ridge_right_sum <= 0;
            ridge_first_gray <= 0; ridge_last_gray <= 0; ridge_peak <= 0; ridge_end <= 0;
            ridge_seen_x <= 0; ridge_seen_y <= 0;
            if (rst) begin cap_ridge_flags <= 0; ridge_flags <= 0; end
            // SOF may coincide with native pixel zero; seed history only.
            // A support beginning at this pixel cannot have a full left ROI window.
            if (!rst && pixel_valid) begin
                ridge_bad <= line_bad;
                ridge_hist[0] <= gray; ridge_hist_count <= 1;
                ridge_seen <= 1; ridge_seen_x <= pixel_x; ridge_seen_y <= pixel_y;
                if (pixel_x >= roi_x0 && pixel_x < roi_x1 &&
                    pixel_y >= roi_y0 && pixel_y < roi_y1 && gray > threshold) begin
                    ridge_have <= 1; ridge_end <= pixel_x;
                    ridge_start <= pixel_x; ridge_components <= 1; ridge_in_support <= 1;
                    ridge_first_gray <= gray; ridge_last_gray <= gray; ridge_peak <= gray;
                end
            end
        end else begin
            rl = ridge_left_sum; rr = ridge_right_sum; rc = ridge_right_count;
            rstart = ridge_start; rend = ridge_end; rcomponents = ridge_components;
            rin = ridge_in_support; rfirst = ridge_first_gray; rlast = ridge_last_gray;
            rpeak = ridge_peak; rhave = ridge_have; rleft = ridge_left_complete;
            rbad = ridge_bad | line_bad;
            if (pixel_valid) begin
                if (ridge_seen && (pixel_x != ridge_seen_x+1'b1 || pixel_y != ridge_seen_y))
                    rbad = 1;
                for (ri=10; ri>0; ri=ri-1) ridge_hist[ri] <= ridge_hist[ri-1];
                ridge_hist[0] <= gray;
                if (ridge_hist_count < 11) ridge_hist_count <= ridge_hist_count+1'b1;
                ridge_seen <= 1; ridge_seen_x <= pixel_x; ridge_seen_y <= pixel_y;
                if (pixel_x >= roi_x0 && pixel_x < roi_x1 &&
                    pixel_y >= roi_y0 && pixel_y < roi_y1) begin
                    if (gray > threshold) begin
                        if (!rin) rcomponents = rcomponents+1;
                        if (!rhave) begin
                            rl = 0;
                            for (ri=3; ri<11; ri=ri+1) rl = rl+ridge_hist[ri];
                            rleft = ridge_hist_count == 11 && {1'b0,pixel_x} >= {1'b0,roi_x0}+11;
                            rfirst = gray;
                            rstart = pixel_x;
                        end
                        rhave = 1; rin = 1; rend = pixel_x; rlast = gray;
                        if (gray > rpeak) rpeak = gray;
                        rr = 0; rc = 0;
                    end else begin
                        rin = 0;
                        if (rhave && pixel_x >= rend+4 && pixel_x <= rend+11) begin
                            rr = rr+gray; rc = rc+1;
                        end
                    end
                end else rin = 0;
            end
            if (div_busy && div_count == 38) ridge_flags <= cap_ridge_flags;
            if (line_end) begin
                rcomplete = rhave && rleft && rc == 8 && rend+11 < roi_x1 && !rbad;
                rcontrast = 8*rpeak >= ((rl > rr ? rl : rr)+160);
                // Eight-DN drops imply that a maximum lies strictly inside support.
                rshape = rhave && rpeak-rfirst >= 8 && rpeak-rlast >= 8 &&
                         rend-rstart+1 >= 3;
                if (!div_busy) begin
                    cap_ridge_flags <= (rcomplete && rcomponents == 1 &&
                        !pending_overrun &&
                        line_y >= roi_y0 && line_y < roi_y1)
                        ? {rshape,rcontrast,1'b1} : 3'd0;
                end
                ridge_hist_count <= 0; ridge_right_count <= 0; ridge_seen <= 0;
                ridge_start <= 0; ridge_components <= 0; ridge_in_support <= 0;
                ridge_bad <= 0; ridge_left_complete <= 0; ridge_have <= 0;
                ridge_left_sum <= 0; ridge_right_sum <= 0; ridge_end <= 0;
                ridge_first_gray <= 0; ridge_last_gray <= 0; ridge_peak <= 0;
            end else begin
                ridge_left_sum <= rl; ridge_right_sum <= rr; ridge_right_count <= rc;
                ridge_end <= rend; ridge_first_gray <= rfirst; ridge_last_gray <= rlast;
                ridge_start <= rstart; ridge_components <= rcomponents; ridge_in_support <= rin;
                ridge_peak <= rpeak; ridge_have <= rhave; ridge_left_complete <= rleft;
                ridge_bad <= rbad;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            acc_raw_peak <= 0; acc_sat250 <= 0;
            cap_raw_peak <= 0; cap_sat250 <= 0;
            xc_all <= 0; roi_raw_peak <= 0; roi_sat250_count <= 0;
            acc_s0          <= 18'd0;
            acc_s1          <= 27'd0;
            acc_peak        <= 8'd0;
            acc_first_x     <= 10'd0;
            acc_last_x      <= 10'd0;
            acc_candidates  <= 10'd0;
            acc_saturated <= 10'd0;
            acc_in_support  <= 1'b0;
            acc_have_support <= 1'b0;
            acc_bad         <= 1'b0;
            acc_roi_outside <= 1'b0;
            cap_s0          <= 18'd0;
            cap_s1          <= 27'd0;
            cap_peak        <= 8'd0;
            cap_width       <= 10'd0;
            cap_candidates  <= 10'd0;
            cap_first_x <= 0; cap_last_x <= 0; cap_saturated <= 0;
            first_support_x <= 0; last_support_x <= 0; saturated_support_count <= 0;
            cap_row         <= 10'd0;
            cap_bad         <= 1'b0;
            cap_roi_outside <= 1'b0;
            cap_overrun     <= 1'b0;
            div_busy        <= 1'b0;
            div_count       <= 6'd0;
            div_num         <= 39'd0;
            div_den         <= 18'd1;
            div_rem         <= 19'd0;
            div_quot        <= 39'd0;
            pending_overrun <= 1'b0;
            row_valid       <= 1'b0;
            row             <= 10'd0;
            xc_fixed        <= 22'd0;
            s0              <= 18'd0;
            s1              <= 27'd0;
            peak            <= 8'd0;
            width           <= 10'd0;
            candidate_count <= 10'd0;
            flags           <= 16'd0;
            quality         <= 8'd0;
            valid           <= 1'b0;
            diagnostics    <= 16'd0;
            overrun         <= 1'b0;
        end else begin
            row_valid <= 1'b0;

            if (frame_start) begin
                if (div_busy)
                    overrun <= 1'b1;
                else
                    overrun <= 1'b0;
                div_busy        <= 1'b0;
                div_count       <= 6'd0;
                pending_overrun <= 1'b0;
                acc_raw_peak <= 0; acc_sat250 <= 0;
                acc_s0          <= 18'd0;
                acc_s1          <= 27'd0;
                acc_peak        <= 8'd0;
                acc_first_x     <= 10'd0;
                acc_last_x      <= 10'd0;
                acc_candidates  <= 10'd0;
            acc_saturated <= 10'd0;
                acc_in_support  <= 1'b0;
                acc_have_support <= 1'b0;
                acc_bad         <= 1'b0;
                acc_roi_outside <= 1'b0;

                // The receiver may assert SOF together with the first
                // pixel (FV and LV can rise on the same camera clock).  Do
                // not lose that pixel while clearing the previous frame's
                // accumulators.  The common line-end path below handles all
                // subsequent pixels; this small seed keeps the interface
                // valid for both SOF-only and SOF+pixel producers.
                if (pixel_valid) begin
                    if ((pixel_x >= roi_x0) && (pixel_x < roi_x1) &&
                        (pixel_y >= roi_y0) && (pixel_y < roi_y1)) begin
                        acc_raw_peak <= gray;
                        acc_sat250 <= (gray >= 8'd250) ? 10'd1 : 10'd0;
                        if (gray > threshold) begin
                            acc_s0           <= {10'd0, (gray - threshold)};
                            acc_s1           <= {{17{1'b0}}, pixel_x} *
                                                 {{19{1'b0}}, (gray - threshold)};
                            acc_peak         <= gray - threshold;
                            acc_first_x      <= pixel_x;
                            acc_last_x       <= pixel_x;
                            acc_candidates   <= 10'd1;
                            acc_saturated <= (gray == 8'hff) ? 10'd1 : 10'd0;
                            acc_in_support   <= 1'b1;
                            acc_have_support <= 1'b1;
                        end
                    end else if ((pixel_y < roi_y0) || (pixel_y >= roi_y1)) begin
                        acc_roi_outside <= 1'b1;
                    end
                    acc_bad <= line_bad;
                end
            end else begin
                // Build the state after this cycle's pixel.  A pixel outside
                // the half-open ROI is a spatial gap, not part of support.
                next_raw_peak = acc_raw_peak;
                next_sat250 = acc_sat250;
                next_s0           = acc_s0;
                next_s1           = acc_s1;
                next_peak         = acc_peak;
                next_first_x      = acc_first_x;
                next_last_x       = acc_last_x;
                next_candidates   = acc_candidates;
                next_saturated = acc_saturated;
                next_in_support   = acc_in_support;
                next_have_support = acc_have_support;
                next_bad          = acc_bad | line_bad;
                next_roi_outside  = acc_roi_outside;
                weight_tmp        = 9'd0;
                product_tmp       = 27'd0;
                inside_roi_tmp    = 1'b0;

                if (pixel_valid) begin
                    inside_roi_tmp = (pixel_x >= roi_x0) && (pixel_x < roi_x1) &&
                                     (pixel_y >= roi_y0) && (pixel_y < roi_y1);
                    if (!inside_roi_tmp)
                        next_in_support = 1'b0;
                    if ((pixel_y < roi_y0) || (pixel_y >= roi_y1))
                        next_roi_outside = 1'b1;
                    if (inside_roi_tmp) begin
                        if (gray > acc_raw_peak) next_raw_peak = gray;
                        if (gray >= 8'd250 && acc_sat250 != 10'h3ff)
                            next_sat250 = acc_sat250 + 10'd1;
                        if (gray > threshold)
                            weight_tmp = {1'b0, gray} - {1'b0, threshold};
                        if (weight_tmp != 0) begin
                            product_tmp = {{18{1'b0}}, weight_tmp} *
                                          {{17{1'b0}}, pixel_x};
                            if ((gray == 8'hff) && acc_saturated != 10'h3ff)
                                next_saturated = acc_saturated + 10'd1;
                            next_s0 = acc_s0 + weight_tmp;
                            next_s1 = acc_s1 + product_tmp;
                            if (!acc_in_support) begin
                                if (acc_candidates != 10'h3ff)
                                    next_candidates = acc_candidates + 10'd1;
                                if (!acc_have_support)
                                    next_first_x = pixel_x;
                            end
                            next_last_x       = pixel_x;
                            next_in_support   = 1'b1;
                            next_have_support = 1'b1;
                            if (weight_tmp[7:0] > acc_peak)
                                next_peak = weight_tmp[7:0];
                        end else begin
                            next_in_support = 1'b0;
                        end
                    end
                end

                // Advance the restoring divider in parallel with the next
                // row.  The final quotient uses the just-computed shift value.
                if (div_busy) begin
                    rem_shift_tmp  = {div_rem[17:0], div_num[38]};
                    num_shift_tmp  = {div_num[37:0], 1'b0};
                    quot_shift_tmp = {div_quot[37:0], 1'b0};
                    if (rem_shift_tmp >= {1'b0, div_den}) begin
                        rem_shift_tmp   = rem_shift_tmp - {1'b0, div_den};
                        quot_shift_tmp[0] = 1'b1;
                    end
                    div_rem  <= rem_shift_tmp;
                    div_num  <= num_shift_tmp;
                    div_quot <= quot_shift_tmp;
                    if (div_count == 6'd38) begin
                        flags_tmp       = 16'd0;
                        diagnostics_tmp = 16'd0;
                        // Rows outside the half-open y ROI are intentionally
                        // emitted as invalid records with only DIAG_ROI set.
                        // They have no photometric support by definition, so
                        // ZERO_SUM/WEAK/WIDE/MULTI would be a false physical
                        // diagnosis and would disagree with gray_reference.
                        // BAD and OVERRUN are independent transport/input
                        // faults and are added below even for an excluded row.
                        if (!cap_roi_outside) begin
                            if (cap_s0 == 0)
                                flags_tmp[FLAG_ZERO_SUM] = 1'b1;
                            if ((cap_s0 < min_sum) || (cap_peak < min_peak))
                                flags_tmp[FLAG_WEAK] = 1'b1;
                            if (cap_width > max_width)
                                flags_tmp[FLAG_WIDE] = 1'b1;
                            if (cap_candidates > 10'd1)
                                flags_tmp[FLAG_MULTI] = 1'b1;
                        end
                        if (cap_bad)
                            flags_tmp[FLAG_BAD] = 1'b1;
                        if (cap_overrun)
                            flags_tmp[FLAG_OVERRUN] = 1'b1;
                        // A zero-support row has no physical stripe width.
                        // Keep its no-support/weak flags without also
                        // reporting "too narrow"; the Python reference
                        // reserves DIAG_NARROW for a present support run
                        // whose measured width is below the configured
                        // minimum.
                        if ((cap_s0 != 0) && (cap_width < min_width))
                            diagnostics_tmp[DIAG_NARROW] = 1'b1;
                        if (cap_roi_outside)
                            diagnostics_tmp[DIAG_ROI] = 1'b1;
                        valid_tmp = (flags_tmp == 16'd0) &&
                                    (diagnostics_tmp == 16'd0) &&
                                    (cap_s0 != 0);

                        xc_all <= (cap_s0 != 0) ? quot_shift_tmp[21:0] : 22'd0;
                        roi_raw_peak <= cap_raw_peak;
                        roi_sat250_count <= cap_sat250;
                        row_valid       <= 1'b1;
                        row             <= cap_row;
                        xc_fixed        <= valid_tmp ? quot_shift_tmp[21:0] : 22'd0;
                        s0              <= cap_s0;
                        s1              <= cap_s1;
                        peak            <= cap_peak;
                        width           <= cap_width;
                        candidate_count <= cap_candidates;
                        first_support_x <= cap_first_x;
                        last_support_x <= cap_last_x;
                        saturated_support_count <= cap_saturated;
                        flags           <= flags_tmp;
                        diagnostics     <= diagnostics_tmp;
                        valid           <= valid_tmp;
                        quality         <= valid_tmp ? cap_peak : 8'd0;
                        div_busy        <= 1'b0;
                        div_count       <= 6'd0;
                    end else begin
                        div_count <= div_count + 6'd1;
                    end
                end

                if (line_end) begin
                    if (div_busy) begin
                        // There is no second row-result slot.  Preserve the
                        // new row's pixels only for the next future row and
                        // make this explicit overrun visible.
                        overrun         <= 1'b1;
                        pending_overrun <= 1'b1;
                    end else begin
                        cap_raw_peak <= next_raw_peak; cap_sat250 <= next_sat250;
                        cap_s0           <= next_s0;
                        cap_s1           <= next_s1;
                        cap_peak         <= next_peak;
                        cap_width        <= next_have_support
                            ? (next_last_x - next_first_x + 10'd1) : 10'd0;
                        cap_candidates   <= next_candidates;
                        cap_first_x <= next_have_support ? next_first_x : 10'd0;
                        cap_last_x <= next_have_support ? next_last_x : 10'd0;
                        cap_saturated <= next_saturated;
                        cap_row          <= line_y;
                        cap_bad          <= next_bad;
                        cap_roi_outside  <= next_roi_outside ||
                                            (line_y < roi_y0) || (line_y >= roi_y1);
                        cap_overrun      <= pending_overrun;
                        div_num          <= {next_s1, {FRACTIONAL_BITS{1'b0}}};
                        div_den          <= (next_s0 == 0) ? 18'd1 : next_s0;
                        div_rem          <= 19'd0;
                        div_quot         <= 39'd0;
                        div_count        <= 6'd0;
                        div_busy         <= 1'b1;
                        pending_overrun  <= 1'b0;
                    end
                    acc_raw_peak <= 0; acc_sat250 <= 0;
                    acc_s0           <= 18'd0;
                    acc_s1           <= 27'd0;
                    acc_peak         <= 8'd0;
                    acc_first_x      <= 10'd0;
                    acc_last_x       <= 10'd0;
                    acc_candidates   <= 10'd0;
                    acc_saturated <= 10'd0;
                    acc_in_support   <= 1'b0;
                    acc_have_support <= 1'b0;
                    acc_bad          <= 1'b0;
                    acc_roi_outside  <= 1'b0;
                end else begin
                    acc_raw_peak <= next_raw_peak; acc_sat250 <= next_sat250;
                    acc_s0           <= next_s0;
                    acc_s1           <= next_s1;
                    acc_peak         <= next_peak;
                    acc_first_x      <= next_first_x;
                    acc_last_x       <= next_last_x;
                    acc_candidates  <= next_candidates;
                    acc_saturated <= next_saturated;
                    acc_in_support   <= next_in_support;
                    acc_have_support <= next_have_support;
                    acc_bad          <= next_bad;
                    acc_roi_outside  <= next_roi_outside;
                end
            end
        end
    end
endmodule
