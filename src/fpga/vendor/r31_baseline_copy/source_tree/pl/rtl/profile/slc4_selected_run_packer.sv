`timescale 1ns/1ps

// SLC1 / NLQ1. Histograms have synchronous read/modify/write ports and a
// bounded sequential clear. Only identity/order checked complete frames emit.
// NLQ1 is the legacy diagnostic policy, not final v3/geometry acceptance.
module slc4_selected_run_packer #(
    parameter integer MAX_POINTS = 480,
    parameter integer FRAME_WIDTH = 752,
    parameter integer FRAME_HEIGHT = 480
) (
    input logic clk, rst, start, abort,
    input logic segment_reset,
    output logic segment_sticky_error, segment_busy,
    input logic [31:0] profile_id, angle,
    input logic [15:0] epoch,
    input logic [63:0] timestamp,
    input logic [7:0] threshold, min_peak,
    input logic [9:0] roi_x0, roi_x1, roi_y0, roi_y1, min_width, max_width,
    input logic [17:0] min_sum,
    input logic point_valid,
    input logic ridge_mode,
    input logic [351:0] point_record,
    input logic [42:0] point_extra,
    input logic [63:0] point_selected,
    input logic finish,
    input logic [15:0] frame_errors,
    input logic tready,
    output logic row_ready,
    output logic busy, done,
    output logic [31:0] tdata,
    output logic tvalid, tlast, error
);
    typedef enum logic [4:0] {IDLE, CLEAR, CAPTURE, HIST_READ, HIST_WRITE,
        SCAN_READ, SCAN_ADD, DECIDE, SEG_START, SEG_LOAD, SEG_SEND,
        SEG_FINISH, SEG_COLLECT, SEG_FALLBACK, HEADER, LOAD, ROWS,
        SEG_REGISTER} state_t;
    state_t state;
    (* ram_style = "block" *) logic [159:0] row_mem [0:MAX_POINTS-1];
    (* ram_style = "block" *) logic [8:0] raw_hist [0:255];
    (* ram_style = "block" *) logic [8:0] peak_hist [0:255];
    (* ram_style = "block" *) logic [8:0] width_hist [0:1023];
    logic [159:0] row_q;
    // Isolate the row BRAM clock-to-output from the segment association
    // arithmetic. Original packet output retains its existing row_q path.
    logic [159:0] segment_row_q;
    (* ram_style = "block" *) logic [31:0] decision_mem [0:MAX_POINTS-1];
    logic [31:0] decision_q;
    logic [9:0] segment_index, segment_write_index;
    logic [13:0] segment_watchdog;
    logic [8:0] raw_q, peak_q, width_q;
    logic [9:0] clear_index, scan_index, frame_rows, roi_count, good_count;
    logic [9:0] sat_rows, multi_rows, preaccepted, max_valid_width;
    logic [18:0] sat_pixels;
    logic [9:0] read_index;
    logic [4:0] header_index;
    logic [2:0] phase;
    logic [31:0] id_latched, angle_latched;
    logic ridge_mode_latched;
    logic [15:0] epoch_latched, errors_latched;
    logic [63:0] time_latched;
    logic [7:0] threshold_latched, peak_latched;
    logic [9:0] x0_latched, x1_latched, y0_latched, y1_latched;
    logic [9:0] minw_latched, maxw_latched;
    logic [17:0] sum_latched;
    logic [7:0] pending_raw, pending_peak;
    logic [9:0] pending_width;
    logic pending_good;
    logic [9:0] raw_cum, peak_cum, width_cum;
    logic [9:0] raw_low, raw_high, peak_low, peak_high, width_low, width_high;
    logic [2:0] quality_status;
    wire [9:0] expected_rows = y1_latched - y0_latched;
    wire [19:0] roi_area = (x1_latched-x0_latched)*expected_rows;
    // Geometry is stable throughout CLEAR; precompute the exact legacy
    // 20-bit area before capture without changing packet/state latency.
    logic [19:0] roi_area_latched;
    wire [9:0] raw_lower_rank = ((expected_rows-1'b1)>>1)+1'b1;
    wire [9:0] raw_upper_rank = (expected_rows>>1)+1'b1;
    wire [9:0] good_lower_rank = ((good_count-1'b1)>>1)+1'b1;
    wire [9:0] good_upper_rank = (good_count>>1)+1'b1;
    wire [10:0] raw_median2 = {1'b0,raw_low}+{1'b0,raw_high};
    wire [10:0] peak_median2 = {1'b0,peak_low}+{1'b0,peak_high};
    wire [10:0] width_median2 = {1'b0,width_low}+{1'b0,width_high};
    wire [31:0] p_row = point_record[31:0];
    wire [31:0] p_s0 = point_record[95:64];
    wire [31:0] p_peak = point_record[159:128];
    wire [31:0] p_width = point_record[191:160];
    wire [31:0] p_components = point_record[223:192];
    wire p_good = point_record[247];
    wire p_fault = |point_record[237:236];
    wire [9:0] p_first = point_record[329:320];
    wire [9:0] p_last = point_record[339:330];
    wire [9:0] p_sat255 = point_record[349:340];
    wire p_single = p_components == 1 && !p_fault;
    wire p_fallback = p_components > 1 || p_fault;
    wire in_roi = p_row >= y0_latched && p_row < y1_latched;
    wire selected_bad;
    wire point_bad = frame_rows >= FRAME_HEIGHT || p_row != frame_rows ||
        point_record[319:288] != id_latched || p_width > FRAME_WIDTH ||
        p_components > FRAME_WIDTH || p_peak > 255 ||
        point_record[351:350] != 0 || point_extra[39:30] > (x1_latched-x0_latched) || selected_bad;
    logic [4:0] classification;
    logic [95:0] compact_record;
    always_comb begin
        classification = 0;
        if (p_single) begin
            if (p_width < minw_latched || p_width > maxw_latched) classification[0] = 1;
            if (p_s0 < sum_latched || p_peak < peak_latched || p_s0 == 0 || p_peak == 0)
                classification[1] = 1;
            if (p_sat255 != 0) classification[2] = 1;
            if (p_first == x0_latched || p_last == x1_latched-1'b1) classification[3] = 1;
            // Geometry <=752x480 makes 18/27/22-bit support/Q12 overflow
            // impossible for an honest gray8 producer; retain the ABI bit.
            if (p_s0 > 32'h3ffff || point_record[127:96] > 32'h7ffffff ||
                point_extra[21:0] >= FRAME_WIDTH*4096) classification[4] = 1;
        end
        compact_record = 0;
        compact_record[21:0] = p_single ? point_extra[21:0] : 22'd0;
        compact_record[26:22] = classification;
        compact_record[27] = p_good;
        compact_record[28] = p_fallback;
        compact_record[29] = p_fault;
        compact_record[41:32] = p_first;
        compact_record[51:42] = p_last;
        compact_record[61:52] = p_components[9:0];
        compact_record[73:64] = p_width[9:0];
        compact_record[81:74] = point_extra[29:22];
        compact_record[89:82] = p_peak[7:0];
        compact_record[90] = p_sat255 != 0;
        compact_record[93:91] = (ridge_mode_latched && p_single) ? point_extra[42:40] : 3'd0;
    end
    // URC1 is separate from the unchanged original96-bit RPG record.
    // Single-run rows preserve even rejected legacy candidate facts; multi
    // rows use only the explicitly supported unique-eligible candidate.
    logic [63:0] selected_record;
    always_comb begin
        selected_record=point_selected;
        if (p_fault) selected_record[63]=0;
        if (selected_record[63] && p_components<=1) begin
            selected_record[31:0]=0;
            selected_record[51:32]=0;
            if (p_single) begin
                selected_record[30]=1;
                selected_record[29:27]=compact_record[93:91];
                selected_record[26:22]=classification;
                selected_record[21:0]=compact_record[21:0];
                selected_record[41:32]=p_first;
                selected_record[51:42]=p_last;
            end
        end
        if (!selected_record[63]) begin
            selected_record[31:0]=0;
            selected_record[51:32]=0;
        end
    end
    assign selected_bad=selected_record[31] ||
        selected_record[62]!=(p_components>8) ||
        (selected_record[63] && (p_fault || selected_record[62] ||
          (p_components>1 && (selected_record[60:57]!=1 || selected_record[61] ||
            !selected_record[30] || selected_record[26:22]!=0)))) ||
        (selected_record[30] && (selected_record[41:32]<x0_latched ||
          selected_record[51:42]>=x1_latched || selected_record[41:32]>selected_record[51:42] ||
          selected_record[21:0]<({12'd0,selected_record[41:32]}<<12) ||
          selected_record[21:0]>({12'd0,selected_record[51:42]}<<12) ||
          (!selected_record[27] && selected_record[29:27]!=0)));
    // This engine is in the existing system domain, after row CDC. Its
    // sticky fault reset is deliberately independent of automatic capture flush.
    wire seg_start_ready, seg_row_ready, seg_finish_ready, seg_valid, seg_done;
    wire seg_accept, seg_retained, seg_last, seg_fallback;
    wire [3:0] seg_reason, seg_fallback_reason;
    wire [31:0] seg_request_id;
    wire [15:0] seg_epoch;
    wire [2:0] seg_quality;
    wire [78:0] seg_record;
    wire preheader_active = state == SCAN_READ || state == SCAN_ADD || state == DECIDE ||
        state == SEG_START || state == SEG_LOAD || state == SEG_REGISTER || state == SEG_SEND ||
        state == SEG_FINISH || state == SEG_COLLECT || state == SEG_FALLBACK;
    assign segment_busy = busy || (!seg_start_ready && !segment_sticky_error && !segment_reset && !abort);
    always_ff @(posedge clk) begin
        if (rst || abort || start || state == HEADER || state == IDLE) segment_watchdog <= 0;
        else if (preheader_active) segment_watchdog <= segment_watchdog+1'b1;
    end
    r14_primary_segment_finalizer u_segments (
        .clk(clk), .rst(segment_reset), .abort(abort),
        .start_valid(state == SEG_START && !rst && !abort), .start_ready(seg_start_ready),
        .start_request_id(id_latched), .start_epoch(epoch_latched), .start_quality_status(quality_status),
        .roi_x0(x0_latched), .roi_x1(x1_latched), .roi_y0(y0_latched), .roi_y1(y1_latched),
        .cfg_threshold(threshold_latched), .cfg_min_peak(peak_latched), .cfg_saturation(8'd255),
        .cfg_min_width(minw_latched), .cfg_max_width(maxw_latched), .cfg_row_gap(10'd1),
        .cfg_min_segment(10'd3), .cfg_width_span(10'd12), .cfg_min_sum(sum_latched),
        .cfg_distance_q12(22'd12288), .cfg_ridge_contrast(8'd20), .cfg_ridge_drop(8'd8),
        .row_valid(state == SEG_SEND && !rst && !abort), .row_ready(seg_row_ready),
        .row_request_id(id_latched), .row_epoch(epoch_latched), .row_y(y0_latched+segment_index),
        // This is an INTERNAL selected-candidate record, not source RPG count.
        // Unsupported rows explicitly supply count2 to request frame fallback.
        .row_center_q12(segment_row_q[117:96]), .row_first(segment_row_q[137:128]), .row_last(segment_row_q[147:138]),
        .row_count(!segment_row_q[159] ? 10'd2 : (segment_row_q[126] ? 10'd1 : 10'd0)),
        .row_fault(segment_row_q[29]), .row_classification(segment_row_q[122:118]),
        .row_ridge(segment_row_q[125:123]), .row_raw_peak(segment_row_q[81:74]),
        .finish_valid(state == SEG_FINISH && !rst && !abort), .finish_ready(seg_finish_ready),
        .finish_request_id(id_latched), .finish_epoch(epoch_latched),
        .out_valid(seg_valid), .out_ready(state == SEG_COLLECT && !rst && !abort),
        .out_request_id(seg_request_id), .out_epoch(seg_epoch), .out_quality_status(seg_quality),
        .out_record(seg_record), .out_reason(seg_reason), .out_accept(seg_accept),
        .out_retained(seg_retained), .out_last(seg_last), .done(seg_done),
        .frame_fallback(seg_fallback), .fallback_reason(seg_fallback_reason),
        .sticky_error(segment_sticky_error)
    );

    // Each histogram has exactly one write port and one synchronous read
    // port. Clear/RMW and RMW/scan addresses are multiplexed before RAM;
    // no second procedural memory assignment is used for either operation.
    wire hist_read_enable = state == HIST_READ || state == SCAN_READ;
    wire hist_clear = state == CLEAR;
    wire hist_update = state == HIST_WRITE && !rst && !abort;
    wire raw_write_enable = (hist_clear && clear_index < 256) || hist_update;
    wire selected_write_enable = (hist_clear && clear_index < 256) ||
        (hist_update && pending_good);
    wire width_write_enable = hist_clear || (hist_update && pending_good);
    wire [7:0] raw_write_addr = hist_clear ? clear_index[7:0] : pending_raw;
    wire [7:0] peak_write_addr = hist_clear ? clear_index[7:0] : pending_peak;
    wire [9:0] width_write_addr = hist_clear ? clear_index : pending_width;
    wire [8:0] raw_write_data = hist_clear ? 9'd0 : raw_q+9'd1;
    wire [8:0] peak_write_data = hist_clear ? 9'd0 : peak_q+9'd1;
    wire [8:0] width_write_data = hist_clear ? 9'd0 : width_q+9'd1;
    wire [7:0] raw_read_addr = state == SCAN_READ ? scan_index[7:0] : pending_raw;
    wire [7:0] peak_read_addr = state == SCAN_READ ? scan_index[7:0] : pending_peak;
    wire [9:0] width_read_addr = state == SCAN_READ ? scan_index : pending_width;
    // Raw/peak histograms have256bins. Mask their registered output during
    // the width-only tail of the scan instead of masking the memory read.
    wire [8:0] raw_scan_count = scan_index < 256 ? raw_q : 9'd0;
    wire [8:0] peak_scan_count = scan_index < 256 ? peak_q : 9'd0;
    // Multiplex operation addresses before each physical RAM port. Separate
    // procedural accesses with mutually exclusive state tests can otherwise
    // be inferred as extra ports and dissolve these frame stores into FFs.
    wire row_write_enable = state == CAPTURE && point_valid && in_roi && !point_bad && !rst && !abort;
    wire row_read_enable = state == LOAD || state == SEG_LOAD;
    wire [9:0] row_read_addr = state == SEG_LOAD ? segment_index : read_index;
    wire decision_fallback_write = state == SEG_FALLBACK;
    wire decision_write_enable = !rst && !abort &&
        (decision_fallback_write || (state == SEG_COLLECT && seg_valid));
    wire [9:0] decision_write_addr = decision_fallback_write ? segment_index : segment_write_index;
    wire [31:0] decision_write_data = decision_fallback_write ? 32'h00000080 :
        {24'd0,1'b0,1'b1,seg_reason,seg_retained,seg_accept};
    always_ff @(posedge clk) begin
        if (raw_write_enable) raw_hist[raw_write_addr] <= raw_write_data;
        if (selected_write_enable) peak_hist[peak_write_addr] <= peak_write_data;
        if (width_write_enable) width_hist[width_write_addr] <= width_write_data;
        if (hist_read_enable) begin
            raw_q <= raw_hist[raw_read_addr];
            peak_q <= peak_hist[peak_read_addr];
            width_q <= width_hist[width_read_addr];
        end
    end
    always_ff @(posedge clk) begin
        if (row_write_enable) row_mem[roi_count] <= {selected_record,compact_record};
        if (row_read_enable) row_q <= row_mem[row_read_addr];
    end
    always_ff @(posedge clk) begin
        if (rst || abort) segment_row_q <= 0;
        else if (state == SEG_REGISTER) segment_row_q <= row_q;
    end
    always_ff @(posedge clk) begin
        if (decision_write_enable) decision_mem[decision_write_addr] <= decision_write_data;
        if (state == LOAD) decision_q <= decision_mem[read_index];
    end
    assign row_ready = state == CAPTURE && !point_valid;
    assign busy = state != IDLE;
    assign tvalid = state == HEADER || state == ROWS;
    assign tlast = state == ROWS && phase == 5 && read_index == roi_count-1'b1;
    always_comb begin
        tdata = 0;
        if (state == HEADER) case (header_index)
            0: tdata = 32'h534c4334;
            1: tdata = 32'h00010000;
            2: tdata = 32+6*roi_count;
            3: tdata = id_latched;
            4: tdata = {16'd0,epoch_latched};
            5: tdata = angle_latched;
            6: tdata = time_latched[31:0];
            7: tdata = time_latched[63:32];
            8: tdata = good_count;
            9: tdata = {16'd0,errors_latched};
            10: tdata = roi_count;
            11: tdata = FRAME_WIDTH;
            12: tdata = FRAME_HEIGHT;
            13: tdata = threshold_latched;
            14: tdata = {12'd0,x1_latched,x0_latched};
            15: tdata = {12'd0,y1_latched,y0_latched};
            16: tdata = {12'd0,maxw_latched,minw_latched};
            17: tdata = sum_latched;
            18: tdata = {16'd0,8'hff,peak_latched};
            19: tdata = 32'h4e4c5131;
            20: tdata = expected_rows;
            21: tdata = sat_pixels;
            22: tdata = sat_rows;
            23: tdata = multi_rows;
            24: tdata = raw_median2;
            25: tdata = good_count == 0 ? 0 : peak_median2;
            26: tdata = good_count == 0 ? 0 : width_median2;
            27: tdata = max_valid_width;
            28: tdata = {29'd0,quality_status};
            29: tdata = preaccepted;
            30: tdata = 32'h55524331;
            default: tdata = 0;
        endcase
        else if (state == ROWS) begin
            if (phase<3) tdata=row_q[phase*32 +: 32];
            else if (phase==3) tdata=decision_q;
            else if (phase==4) tdata=row_q[127:96];
            else tdata=row_q[159:128];
        end
    end
    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE; done <= 0; error <= 0;
            ridge_mode_latched <= 0;
            clear_index <= 0; scan_index <= 0; frame_rows <= 0; roi_count <= 0;
            good_count <= 0; sat_pixels <= 0; sat_rows <= 0; multi_rows <= 0;
            preaccepted <= 0; max_valid_width <= 0; read_index <= 0;
            header_index <= 0; phase <= 0; id_latched <= 0; epoch_latched <= 0;
            angle_latched <= 0; time_latched <= 0; errors_latched <= 0;
            threshold_latched <= 0; peak_latched <= 0; sum_latched <= 0;
            x0_latched <= 0; x1_latched <= 0; y0_latched <= 0; y1_latched <= 0;
            minw_latched <= 0; maxw_latched <= 0;
            pending_raw <= 0; pending_peak <= 0; pending_width <= 0; pending_good <= 0;
            raw_cum <= 0; peak_cum <= 0; width_cum <= 0;
            raw_low <= 0; raw_high <= 0; peak_low <= 0; peak_high <= 0;
            width_low <= 0; width_high <= 0; quality_status <= 5;
            segment_index <= 0; segment_write_index <= 0;
            roi_area_latched <= 20'd0;
        end else begin
            done <= 0;
            if (abort) begin state <= IDLE; if (state != IDLE) error <= 1; end
            else if (start && state != IDLE) begin state <= IDLE; error <= 1; end
            else if (preheader_active && segment_watchdog >= 8191) begin
                state <= IDLE; error <= 1;
            end else case (state)
                IDLE: if (start) begin
                    state <= CLEAR; clear_index <= 0; error <= 0;
                    frame_rows <= 0; roi_count <= 0; good_count <= 0;
                    sat_pixels <= 0; sat_rows <= 0; multi_rows <= 0;
                    preaccepted <= 0; max_valid_width <= 0;
                    raw_cum <= 0; peak_cum <= 0; width_cum <= 0;
                    raw_low <= 0; raw_high <= 0; peak_low <= 0; peak_high <= 0;
                    width_low <= 0; width_high <= 0;
                    read_index <= 0; header_index <= 0; phase <= 0;
                    id_latched <= profile_id; epoch_latched <= epoch;
                    ridge_mode_latched <= 1'b1;
                    angle_latched <= angle; time_latched <= timestamp;
                    errors_latched <= 0; quality_status <= 5;
                    threshold_latched <= threshold; peak_latched <= min_peak;
                    x0_latched <= roi_x0; x1_latched <= roi_x1;
                    y0_latched <= roi_y0; y1_latched <= roi_y1;
                    minw_latched <= min_width; maxw_latched <= max_width; sum_latched <= min_sum;
                    if (!(roi_x0 < roi_x1 && roi_x1 <= FRAME_WIDTH &&
                          roi_y0 < roi_y1 && roi_y1 <= FRAME_HEIGHT &&
                          roi_y1-roi_y0 <= MAX_POINTS)) begin state <= IDLE; error <= 1; end
                end
                CLEAR: begin
                    if (clear_index == 0) roi_area_latched <= roi_area;
                    if (clear_index == 1023) state <= CAPTURE;
                    else clear_index <= clear_index+1'b1;
                end
                CAPTURE: begin
                    if (point_valid) begin
                        if (point_bad) begin state <= IDLE; error <= 1; end
                        else begin
                            frame_rows <= frame_rows+1'b1;
                            if (in_roi) begin
                                roi_count <= roi_count+1'b1;
                                sat_pixels <= sat_pixels+point_extra[39:30];
                                if (point_extra[39:30] != 0) sat_rows <= sat_rows+1'b1;
                                if (p_components > 1) multi_rows <= multi_rows+1'b1;
                                if (p_single && classification == 0) preaccepted <= preaccepted+1'b1;
                                pending_raw <= point_extra[29:22]; pending_peak <= p_peak[7:0];
                                pending_width <= p_width[9:0]; pending_good <= p_good;
                                if (p_good) begin
                                    good_count <= good_count+1'b1;
                                    if (p_width > max_valid_width) max_valid_width <= p_width[9:0];
                                end
                                state <= HIST_READ;
                            end
                        end
                    end
                    if (finish) begin
                        if (point_valid || frame_rows != FRAME_HEIGHT || roi_count != expected_rows || frame_errors != 0) begin
                            state <= IDLE; error <= 1;
                        end else begin
                            errors_latched <= frame_errors; scan_index <= 0; state <= SCAN_READ;
                        end
                    end
                end
                HIST_READ: state <= HIST_WRITE;
                HIST_WRITE: state <= CAPTURE;
                SCAN_READ: state <= SCAN_ADD;
                SCAN_ADD: begin
                    raw_cum <= raw_cum+raw_scan_count; peak_cum <= peak_cum+peak_scan_count; width_cum <= width_cum+width_q;
                    if (raw_cum < raw_lower_rank && raw_cum+raw_scan_count >= raw_lower_rank) raw_low <= scan_index;
                    if (raw_cum < raw_upper_rank && raw_cum+raw_scan_count >= raw_upper_rank) raw_high <= scan_index;
                    if (good_count != 0) begin
                        if (peak_cum < good_lower_rank && peak_cum+peak_scan_count >= good_lower_rank) peak_low <= scan_index;
                        if (peak_cum < good_upper_rank && peak_cum+peak_scan_count >= good_upper_rank) peak_high <= scan_index;
                        if (width_cum < good_lower_rank && width_cum+width_q >= good_lower_rank) width_low <= scan_index;
                        if (width_cum < good_upper_rank && width_cum+width_q >= good_upper_rank) width_high <= scan_index;
                    end
                    if (scan_index == 1023) state <= DECIDE;
                    else begin scan_index <= scan_index+1'b1; state <= SCAN_READ; end
                end
                DECIDE: begin
                    if (sat_pixels*100 > roi_area_latched || sat_rows*20 > expected_rows) quality_status <= 1;
                    else if (multi_rows*20 > expected_rows || max_valid_width > 32) quality_status <= 2;
                    else if (good_count == 0 && raw_median2 < 120) quality_status <= 3;
                    else if (good_count*5 < expected_rows*4) quality_status <= 4;
                    else if (peak_median2 < 120) quality_status <= 3;
                    else quality_status <= 0;
                    state <= SEG_START;
                end
                SEG_START: if (seg_start_ready) begin
                    segment_index <= 0; segment_write_index <= 0; state <= SEG_LOAD;
                end
                SEG_LOAD: state <= SEG_REGISTER;
                SEG_REGISTER: state <= SEG_SEND;
                SEG_SEND: if (seg_row_ready) begin
                    if (segment_index == roi_count-1'b1) state <= SEG_FINISH;
                    else begin segment_index <= segment_index+1'b1; state <= SEG_LOAD; end
                end
                SEG_FINISH: if (seg_finish_ready) state <= SEG_COLLECT;
                SEG_COLLECT: begin
                    if (seg_valid) begin
                        if (seg_request_id != id_latched || seg_epoch != epoch_latched ||
                            seg_quality != quality_status || segment_write_index >= roi_count ||
                            seg_record[9:0] != y0_latched+segment_write_index) begin
                            state <= IDLE; error <= 1;
                        end else segment_write_index <= segment_write_index+1'b1;
                    end
                    if (seg_done) begin
                        if (segment_sticky_error) begin state <= IDLE; error <= 1; end
                        else if (seg_fallback) begin segment_index <= 0; state <= SEG_FALLBACK; end
                        else if (segment_write_index != roi_count) begin state <= IDLE; error <= 1; end
                        else state <= HEADER;
                    end
                end
                SEG_FALLBACK: begin
                    if (segment_index == roi_count-1'b1) state <= HEADER;
                    else segment_index <= segment_index+1'b1;
                end
                HEADER: if (tready) begin
                    if (header_index == 31) begin state <= LOAD; read_index <= 0; phase <= 0; end
                    else header_index <= header_index+1'b1;
                end
                LOAD: state <= ROWS;
                ROWS: if (tready) begin
                    if (phase == 5) begin
                        phase <= 0;
                        if (read_index == roi_count-1'b1) begin state <= IDLE; done <= 1; end
                        else begin read_index <= read_index+1'b1; state <= LOAD; end
                    end else phase <= phase+1'b1;
                end
                default: state <= IDLE;
            endcase
        end
    end
endmodule
