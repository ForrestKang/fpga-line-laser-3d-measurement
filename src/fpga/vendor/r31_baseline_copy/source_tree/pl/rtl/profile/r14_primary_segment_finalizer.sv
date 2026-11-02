`timescale 1ns/1ps
// Standalone candidate, not connected to the frozen SLC2 wire or any bitfile.
// Receives every ROI row in source order from an already admitted primary-only
// frame. Capture builds segment descriptors; a synchronous BRAM second pass
// publishes final decisions only after complete-frame validation. Original
// records and Q12 are preserved even when the final selection rejects a row.
module r14_primary_segment_finalizer (
    input logic clk, rst, abort,
    input logic start_valid,
    output logic start_ready,
    input logic [31:0] start_request_id,
    input logic [15:0] start_epoch,
    input logic [2:0] start_quality_status,
    input logic [9:0] roi_x0, roi_x1, roi_y0, roi_y1,
    input logic [7:0] cfg_threshold, cfg_min_peak, cfg_saturation,
    input logic [9:0] cfg_min_width, cfg_max_width, cfg_row_gap,
    input logic [9:0] cfg_min_segment, cfg_width_span,
    input logic [17:0] cfg_min_sum,
    input logic [21:0] cfg_distance_q12,
    input logic [7:0] cfg_ridge_contrast, cfg_ridge_drop,
    input logic row_valid,
    output logic row_ready,
    input logic [31:0] row_request_id,
    input logic [15:0] row_epoch,
    input logic [9:0] row_y,
    input logic [21:0] row_center_q12,
    input logic [9:0] row_first, row_last, row_count,
    input logic row_fault,
    input logic [4:0] row_classification,
    input logic [2:0] row_ridge,
    input logic [7:0] row_raw_peak,
    input logic finish_valid,
    output logic finish_ready,
    input logic [31:0] finish_request_id,
    input logic [15:0] finish_epoch,
    output logic out_valid,
    input logic out_ready,
    output logic [31:0] out_request_id,
    output logic [15:0] out_epoch,
    output logic [2:0] out_quality_status,
    output logic [78:0] out_record,
    output logic [3:0] out_reason,
    output logic out_accept, out_retained, out_last,
    output logic done, frame_fallback,
    output logic [3:0] fallback_reason,
    output logic sticky_error
);
    // Reason order is independent of bit positions. Values form a new local
    // module interface only; they do not reinterpret reserved SLC2 bits.
    localparam [3:0] ACCEPT=0, NO_SUPPORT=1, OVERFLOW=2, SATURATED=3,
        ROI_TRUNCATED=4, WIDTH=5, WEAK=6, SHORT_SEGMENT=7,
        WINDOW=8, RIDGE_CONTRAST=9, RIDGE_SHAPE=10, WIDTH_SPAN=11;
    localparam [3:0] FB_CONFIG=1, FB_COMPONENT=2, FB_INPUT=4, FB_ABORT=8;
    typedef enum logic [2:0] {IDLE, CAPTURE, ROW_READ, DESC_READ, EMIT} state_t;
    state_t state;
    // 79-bit original record + 9-bit segment index. Descriptors hold the
    // count of base-eligible rows and ridge-passing width extrema for a segment.
    (* ram_style="block" *) logic [29:0] descriptor_mem [0:479];
    logic [87:0] record_q;
    logic [29:0] descriptor_q;
    logic [31:0] request_latched;
    logic [15:0] epoch_latched;
    logic [2:0] quality_latched;
    logic [9:0] x0_latched, x1_latched, y0_latched, expected_rows;
    logic [9:0] captured, read_index;
    logic [8:0] next_segment, active_segment, active_count;
    logic [9:0] active_min, active_max;
    logic active, active_has_ridge;
    logic [9:0] previous_row;
    logic [21:0] previous_center;
    logic [3:0] fallback_pending;
    wire [78:0] input_record = {row_raw_peak,row_ridge,row_classification,
        row_fault,row_count,row_last,row_first,row_center_q12,row_y};
    wire [9:0] input_width = row_last-row_first+10'd1;
    wire base_eligible = row_count==1 && !row_fault && row_classification==0;
    wire ridge_pass = row_ridge==3'b111;
    wire [21:0] center_distance = row_center_q12>=previous_center ?
        row_center_q12-previous_center : previous_center-row_center_q12;
    wire continue_segment = active && row_y==previous_row+1'b1 &&
        center_distance<=22'd12288;
    wire identity_bad = row_request_id!=request_latched || row_epoch!=epoch_latched;
    wire row_order_bad = captured>=expected_rows || captured>=480 ||
        {1'b0,row_y}!={1'b0,y0_latched}+{1'b0,captured};
    // Fault/multi records intentionally do not carry a usable PL centroid.
    // Their presence requires a whole-frame fallback, not a false malformed-
    // center diagnosis. Identity/order checks still apply independently.
    wire record_bad = (row_fault || row_count>1) ? 1'b0 : row_count==0 ?
        (row_center_q12!=0 || row_first!=0 || row_last!=0 || row_classification!=0 ||
         row_ridge!=0 || row_raw_peak>60) :
        (row_first<x0_latched || row_last>=x1_latched || row_first>row_last ||
         (row_count==1 && (row_center_q12<({12'd0,row_first}<<12) ||
          row_center_q12>({12'd0,row_last}<<12) || row_raw_peak<=60)) ||
         (!row_ridge[0] && row_ridge!=0));
    wire config_supported = roi_x0<roi_x1 && roi_x1<=752 &&
        roi_y0<roi_y1 && roi_y1<=480 && cfg_threshold==60 &&
        cfg_min_width==1 && cfg_max_width==32 && cfg_min_sum==30 &&
        cfg_min_peak==15 && cfg_saturation==255 && cfg_distance_q12==12288 &&
        cfg_row_gap==1 && cfg_min_segment==3 && cfg_width_span==12 &&
        cfg_ridge_contrast==20 && cfg_ridge_drop==8;
    // Protocol/abort faults stop future frames until an explicit reset.
    // Unsupported configuration or multi/fault fallback alone is benign.
    assign start_ready = state==IDLE && !abort && !rst && !sticky_error;
    assign row_ready = state==CAPTURE && !abort && !rst;
    // Rows have priority when both input transactions are offered.
    assign finish_ready = state==CAPTURE && !row_valid && !abort && !rst;
    assign out_valid = state==EMIT && !abort && !rst;
    assign out_request_id = request_latched;
    assign out_epoch = epoch_latched;
    assign out_quality_status = quality_latched;
    assign out_record = record_q[78:0];
    assign out_last = read_index+1'b1==captured;
    wire [9:0] stored_count = record_q[61:52];
    wire [4:0] stored_class = record_q[67:63];
    wire [2:0] stored_ridge = record_q[70:68];
    wire [8:0] segment_count = descriptor_q[8:0];
    wire [9:0] segment_min = descriptor_q[18:9];
    wire [9:0] segment_max = descriptor_q[28:19];
    always_comb begin
        out_reason=ACCEPT;
        if (stored_count==0) out_reason=NO_SUPPORT;
        else if (stored_class[4]) out_reason=OVERFLOW;
        else if (stored_class[2]) out_reason=SATURATED;
        else if (stored_class[3]) out_reason=ROI_TRUNCATED;
        else if (stored_class[0]) out_reason=WIDTH;
        else if (stored_class[1]) out_reason=WEAK;
        else if (segment_count<3) out_reason=SHORT_SEGMENT;
        else if (!stored_ridge[0]) out_reason=WINDOW;
        else if (!stored_ridge[1]) out_reason=RIDGE_CONTRAST;
        else if (!stored_ridge[2]) out_reason=RIDGE_SHAPE;
        else if (descriptor_q[29] && segment_max-segment_min>12) out_reason=WIDTH_SPAN;
        out_retained=out_valid && stored_count==1 && stored_class==0 && segment_count>=3;
        out_accept=out_valid && out_reason==ACCEPT;
    end
    // One write/read port per physical RAM, outside reset/abort/FSM logic.
    // Closing a segment on a new base row, a rejected row, or finish all
    // writes the same active descriptor; mux those enables before the RAM.
    wire record_write_enable = !rst && !abort && state==CAPTURE && row_valid && captured<480;
    wire [8:0] record_write_addr = captured[8:0];
    wire record_read_enable = state==ROW_READ;
    wire [8:0] record_read_addr = read_index[8:0];
    wire [87:0] record_write_data = {base_eligible ?
        (continue_segment ? active_segment : next_segment) : 9'd0,input_record};
    wire descriptor_write_enable = !rst && !abort && state==CAPTURE && active &&
        (row_valid ? (!(base_eligible && captured<480) || !continue_segment) : finish_valid);
    wire [29:0] descriptor_write_data = {active_has_ridge,active_max,active_min,active_count};
    // Synchronous reads preserve the existing ROW_READ/DESC_READ latency.
    // Keep the canonical RAM template in its own hierarchy. The consumer
    // uses the segment-index slice in DESC_READ and the original-record
    // slices in EMIT; flattening can split their read register before RAM
    // mapping. No KEEP/DONT_TOUCH is placed on the RAM output register.
    (* keep_hierarchy="yes" *) r15_segment_record_ram u_record_store (
        .clk(clk), .write_enable(record_write_enable), .write_addr(record_write_addr),
        .write_data(record_write_data), .read_enable(record_read_enable),
        .read_addr(record_read_addr), .read_data(record_q)
    );
    always_ff @(posedge clk) begin
        if (descriptor_write_enable) descriptor_mem[active_segment]<=descriptor_write_data;
        if (state==DESC_READ) descriptor_q<=descriptor_mem[record_q[87:79]];
    end
    always_ff @(posedge clk) begin
        if (rst) begin
            state<=IDLE; captured<=0; read_index<=0; next_segment<=0;
            active_segment<=0; active_count<=0; active_min<=0; active_max<=0;
            active<=0; active_has_ridge<=0; previous_row<=0; previous_center<=0;
            request_latched<=0; epoch_latched<=0; quality_latched<=0; expected_rows<=0;
            x0_latched<=0; x1_latched<=0; y0_latched<=0;
            fallback_pending<=0; done<=0; frame_fallback<=0;
            fallback_reason<=0; sticky_error<=0;
        end else begin
            done<=0;
            if (abort) begin
                if (state!=IDLE) done<=1;
                state<=IDLE; active<=0; captured<=0;
                frame_fallback<=1; fallback_reason<=FB_ABORT; sticky_error<=1;
            end else case (state)
                IDLE: if (start_valid && !sticky_error) begin
                    request_latched<=start_request_id; epoch_latched<=start_epoch;
                    quality_latched<=start_quality_status;
                    x0_latched<=roi_x0; x1_latched<=roi_x1; y0_latched<=roi_y0;
                    expected_rows<=roi_y1-roi_y0;
                    captured<=0; read_index<=0; next_segment<=0; active<=0;
                    active_count<=0; active_has_ridge<=0;
                    fallback_pending<=config_supported ? 4'd0 : FB_CONFIG;
                    frame_fallback<=0; fallback_reason<=0; state<=CAPTURE;
                end
                CAPTURE: begin
                    if (row_valid) begin
                        if (row_count>1 || row_fault)
                            fallback_pending<=fallback_pending | FB_COMPONENT;
                        if (identity_bad || row_order_bad || (!fallback_pending[0] && record_bad)) begin
                            fallback_pending<=fallback_pending | FB_INPUT |
                                ((row_count>1 || row_fault) ? FB_COMPONENT : 4'd0);
                            sticky_error<=1;
                        end
                        if (captured<480) begin
                            captured<=captured+1'b1;
                        end
                        if (base_eligible && captured<480) begin
                            previous_row<=row_y; previous_center<=row_center_q12;
                            if (!continue_segment) begin
                                active<=1; active_segment<=next_segment;
                                next_segment<=next_segment+1'b1; active_count<=1;
                                active_has_ridge<=ridge_pass;
                                active_min<=ridge_pass ? input_width : 10'd1023;
                                active_max<=ridge_pass ? input_width : 10'd0;
                            end else begin
                                active_count<=active_count+1'b1;
                                if (ridge_pass) begin
                                    active_has_ridge<=1;
                                    if (!active_has_ridge || input_width<active_min) active_min<=input_width;
                                    if (!active_has_ridge || input_width>active_max) active_max<=input_width;
                                end
                            end
                        end else if (active) begin
                            active<=0;
                        end
                    end else if (finish_valid) begin
                        active<=0;
                        if (fallback_pending!=0 || captured!=expected_rows || captured==0 ||
                            finish_request_id!=request_latched || finish_epoch!=epoch_latched) begin
                            frame_fallback<=1;
                            fallback_reason<=fallback_pending | ((captured!=expected_rows || captured==0 ||
                                finish_request_id!=request_latched || finish_epoch!=epoch_latched) ? FB_INPUT : 4'd0);
                            if (captured!=expected_rows || captured==0 || finish_request_id!=request_latched ||
                                finish_epoch!=epoch_latched) sticky_error<=1;
                            done<=1; state<=IDLE;
                        end else begin read_index<=0; state<=ROW_READ; end
                    end
                end
                ROW_READ: state<=DESC_READ;
                DESC_READ: state<=EMIT;
                EMIT: if (out_ready) begin
                    if (out_last) begin done<=1; state<=IDLE; end
                    else begin read_index<=read_index+1'b1; state<=ROW_READ; end
                end
                default: begin state<=IDLE; sticky_error<=1; frame_fallback<=1;
                    fallback_reason<=FB_INPUT; done<=1; end
            endcase
        end
    end
endmodule

// Common-clock simple dual-port READ_FIRST template, one-cycle sync read.
// The physical512-word store is bounded to480rows by the parent protocol.
module r15_segment_record_ram (
    input logic clk, write_enable, read_enable,
    input logic [8:0] write_addr, read_addr,
    input logic [87:0] write_data,
    output logic [87:0] read_data
);
    (* ram_style="block" *) logic [87:0] record_mem [0:511];
    always_ff @(posedge clk) begin
        if (write_enable) record_mem[write_addr]<=write_data;
        if (read_enable) read_data<=record_mem[read_addr];
    end
endmodule
