`timescale 1ns/1ps

// SLF1 v1: complete-frame feature records on the existing profile DMA.
// Data RAM has synchronous, reset-free read/write ports. LOAD isolates the
// RAM latency from AXIS, whose data and TLAST remain stable under stalls.
module slf1_feature_packer #(
    parameter integer MAX_POINTS = 480,
    parameter integer FRAME_WIDTH = 752,
    parameter integer FRAME_HEIGHT = 480
) (
    input logic clk, rst, start, abort,
    input logic [31:0] profile_id, angle,
    input logic [15:0] epoch,
    input logic [63:0] timestamp,
    input logic [7:0] threshold, min_peak,
    input logic [9:0] roi_x0, roi_x1, roi_y0, roi_y1, min_width, max_width,
    input logic [17:0] min_sum,
    input logic point_valid,
    input logic [351:0] point_record,
    input logic finish,
    input logic [15:0] frame_errors,
    input logic tready,
    output logic busy, done,
    output logic [31:0] tdata,
    output logic tvalid, tlast, error
);
    localparam integer COUNT_BITS = (MAX_POINTS < 2) ? 1 : $clog2(MAX_POINTS+1);
    typedef enum logic [2:0] {IDLE, CAPTURE, HEADER, LOAD, ROWS} state_t;
    state_t state;
    (* ram_style = "block" *) logic [351:0] row_mem [0:MAX_POINTS-1];
    logic [351:0] row_q;
    logic [COUNT_BITS-1:0] point_count, good_count, read_index;
    logic [4:0] header_index;
    logic [3:0] phase;
    logic [31:0] id_latched, angle_latched;
    logic [15:0] epoch_latched, errors_latched;
    logic [63:0] time_latched;
    logic [7:0] threshold_latched, peak_latched;
    logic [9:0] x0_latched, x1_latched, y0_latched, y1_latched;
    logic [9:0] minw_latched, maxw_latched;
    logic [17:0] sum_latched;
    wire point_bad = point_count >= MAX_POINTS ||
        point_record[31:0] != point_count ||
        point_record[319:288] != id_latched ||
        point_record[255:248] != 0 || point_record[287:260] != 0 ||
        point_record[351:350] != 0;

    always_ff @(posedge clk) begin
        if (state == CAPTURE && point_valid && !point_bad && !rst && !abort)
            row_mem[point_count] <= point_record;
        if (state == LOAD)
            row_q <= row_mem[read_index];
    end

    assign busy = state != IDLE;
    assign tvalid = state == HEADER || state == ROWS;
    assign tlast = (state == HEADER && header_index == 19 && point_count == 0) ||
        (state == ROWS && phase == 10 && read_index == point_count-1'b1);
    always_comb begin
        tdata = 0;
        if (state == HEADER) begin
            case (header_index)
                0: tdata = 32'h534c4631;
                1: tdata = 32'h00010000;
                2: tdata = 20 + 11*point_count;
                3: tdata = id_latched;
                4: tdata = {16'd0,epoch_latched};
                5: tdata = angle_latched;
                6: tdata = time_latched[31:0];
                7: tdata = time_latched[63:32];
                8: tdata = good_count;
                9: tdata = {16'd0,errors_latched};
                10: tdata = point_count;
                11: tdata = FRAME_WIDTH;
                12: tdata = FRAME_HEIGHT;
                13: tdata = {24'd0,threshold_latched};
                14: tdata = {12'd0,x1_latched,x0_latched};
                15: tdata = {12'd0,y1_latched,y0_latched};
                16: tdata = {12'd0,maxw_latched,minw_latched};
                17: tdata = {14'd0,sum_latched};
                18: tdata = {16'd0,8'hff,peak_latched};
                default: tdata = 0;
            endcase
        end else if (state == ROWS)
            tdata = row_q[phase*32 +: 32];
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE; done <= 0; error <= 0;
            point_count <= 0; good_count <= 0; read_index <= 0;
            header_index <= 0; phase <= 0;
            id_latched <= 0; angle_latched <= 0; epoch_latched <= 0;
            time_latched <= 0; errors_latched <= 0;
            threshold_latched <= 0; peak_latched <= 0; sum_latched <= 0;
            x0_latched <= 0; x1_latched <= 0; y0_latched <= 0; y1_latched <= 0;
            minw_latched <= 0; maxw_latched <= 0;
        end else begin
            done <= 0;
            if (abort) begin
                state <= IDLE;
                if (state != IDLE) error <= 1;
            end else case (state)
                IDLE: if (start) begin
                    state <= CAPTURE; error <= 0;
                    point_count <= 0; good_count <= 0; read_index <= 0;
                    header_index <= 0; phase <= 0;
                    id_latched <= profile_id; epoch_latched <= epoch;
                    angle_latched <= angle; time_latched <= timestamp;
                    errors_latched <= frame_errors;
                    threshold_latched <= threshold; peak_latched <= min_peak;
                    x0_latched <= roi_x0; x1_latched <= roi_x1;
                    y0_latched <= roi_y0; y1_latched <= roi_y1;
                    minw_latched <= min_width; maxw_latched <= max_width;
                    sum_latched <= min_sum;
                end
                CAPTURE: begin
                    errors_latched <= frame_errors;
                    if (start || (point_valid && point_bad)) begin
                        state <= IDLE; error <= 1;
                    end else begin
                        if (point_valid) begin
                            point_count <= point_count + 1'b1;
                            if (point_record[247]) good_count <= good_count + 1'b1;
                        end
                        if (finish) begin
                            state <= HEADER; header_index <= 0;
                            read_index <= 0; phase <= 0;
                        end
                    end
                end
                HEADER: if (tready) begin
                    if (header_index == 19) begin
                        if (point_count == 0) begin state <= IDLE; done <= 1; end
                        else state <= LOAD;
                    end else header_index <= header_index + 1'b1;
                end
                LOAD: state <= ROWS;
                ROWS: if (tready) begin
                    if (phase == 10) begin
                        phase <= 0;
                        if (read_index == point_count-1'b1) begin state <= IDLE; done <= 1; end
                        else begin read_index <= read_index+1'b1; state <= LOAD; end
                    end else phase <= phase + 1'b1;
                end
                default: state <= IDLE;
            endcase
        end
    end
endmodule
