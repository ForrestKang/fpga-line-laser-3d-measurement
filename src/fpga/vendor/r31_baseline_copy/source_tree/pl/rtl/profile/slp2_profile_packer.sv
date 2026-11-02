`timescale 1ns/1ps

// SLP2 profile packetizer with a 32-bit AXI4-Stream-like output.
//
// Rows are captured before transmission starts.  This makes the stream
// stable under arbitrary tready backpressure and prevents an unfinished frame
// from being mixed with a later profile.  The packet layout is the frozen P0
// contract: eight header words followed by three words per row.
module slp2_profile_packer #(
    parameter integer MAX_POINTS = 480
) (
    input  logic        clk,
    input  logic        rst,
    input  logic        start,
    input  logic        abort,
    input  logic [31:0] profile_id,
    input  logic [31:0] angle,
    input  logic [63:0] timestamp,
    input  logic        point_valid,
    input  logic [9:0]  point_row,
    input  logic [21:0] point_xc,
    input  logic [7:0]  point_quality,
    input  logic [15:0] point_flags,
    input  logic        point_good,
    input  logic        finish,
    input  logic [15:0] frame_errors,
    input  logic        tready,

    output logic        busy,
    output logic        done,
    output logic [31:0] tdata,
    output logic        tvalid,
    output logic        tlast,
    output logic        error
);

    localparam logic [31:0] SLP2_MAGIC = 32'h534c5032;
    localparam integer COUNT_BITS = (MAX_POINTS < 2) ? 1 : $clog2(MAX_POINTS + 1);

    typedef enum logic [1:0] {IDLE, CAPTURE, SEND} state_t;
    state_t state;

    logic [9:0] row_mem [0:MAX_POINTS-1];
    logic [21:0] xc_mem [0:MAX_POINTS-1];
    logic [7:0] quality_mem [0:MAX_POINTS-1];
    logic [14:0] flags_mem [0:MAX_POINTS-1];
    logic good_mem [0:MAX_POINTS-1];

    logic [COUNT_BITS-1:0] point_count;
    logic [COUNT_BITS-1:0] valid_count;
    logic [COUNT_BITS-1:0] read_index;
    logic [3:0] header_index;
    logic [1:0] point_phase;
    logic in_points;
    logic [31:0] id_latched;
    logic [31:0] angle_latched;
    logic [63:0] timestamp_latched;
    logic [15:0] frame_errors_latched;

    logic [COUNT_BITS-1:0] count_next;
    logic [COUNT_BITS-1:0] valid_count_next;
    logic [31:0] packed_point_word;

    assign busy   = (state != IDLE);
    assign tvalid = (state == SEND);
    assign tlast  = (state == SEND) &&
                    ((!in_points && (header_index == 4'd7) && (point_count == 0)) ||
                     (in_points && (read_index == point_count - 1'b1) &&
                      (point_phase == 2'd2)));

    always @* begin
        tdata = 32'd0;
        packed_point_word = {good_mem[read_index], flags_mem[read_index],
                             quality_mem[read_index]};
        if (state == SEND) begin
            case (header_index)
                4'd0: tdata = SLP2_MAGIC;
                4'd1: tdata = id_latched;
                4'd2: tdata = angle_latched;
                4'd3: tdata = timestamp_latched[31:0];
                4'd4: tdata = timestamp_latched[63:32];
                4'd5: tdata = {{(32-COUNT_BITS){1'b0}}, valid_count};
                4'd6: tdata = {16'd0, frame_errors_latched};
                4'd7: tdata = {{(32-COUNT_BITS){1'b0}}, point_count};
                default: tdata = 32'd0;
            endcase
            if (in_points) begin
                case (point_phase)
                    2'd0: tdata = {22'd0, row_mem[read_index]};
                    2'd1: tdata = {10'd0, xc_mem[read_index]};
                    default: tdata = packed_point_word;
                endcase
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state              <= IDLE;
            point_count        <= '0;
            valid_count        <= '0;
            read_index         <= '0;
            header_index       <= 4'd0;
            point_phase        <= 2'd0;
            in_points          <= 1'b0;
            id_latched         <= 32'd0;
            angle_latched      <= 32'd0;
            timestamp_latched  <= 64'd0;
            frame_errors_latched <= 16'd0;
            done               <= 1'b0;
            error              <= 1'b0;
        end else begin
            done <= 1'b0;

            if (abort && (state != IDLE)) begin
                state <= IDLE;
                error <= 1'b1;
            end else begin
                case (state)
                    IDLE: begin
                        if (start) begin
                            state                <= CAPTURE;
                            point_count          <= '0;
                            valid_count          <= '0;
                            id_latched           <= profile_id;
                            angle_latched        <= angle;
                            timestamp_latched    <= timestamp;
                            frame_errors_latched <= frame_errors;
                            // A new capture is a complete transaction.  A
                            // prior empty packet or abort may have left the
                            // stream cursor at an arbitrary word; reset all
                            // send-side state before accepting rows.
                            header_index         <= 4'd0;
                            point_phase          <= 2'd0;
                            read_index           <= '0;
                            in_points            <= 1'b0;
                            error                <= 1'b0;
                        end
                    end
                    CAPTURE: begin
                        if (start) begin
                            error <= 1'b1;
                        end
                        // Camera EOF diagnostics are allowed to become
                        // known at the final line.  Track the live input
                        // until finish so the emitted header preserves the
                        // final low-16-bit diagnostic value.
                        frame_errors_latched <= frame_errors;

                        // Use next-count values so a point and finish on the
                        // same clock are included in the packet header.
                        count_next       = point_count;
                        valid_count_next = valid_count;
                        if (point_valid) begin
                            if (point_count < MAX_POINTS) begin
                                row_mem[point_count]      <= point_row;
                                xc_mem[point_count]       <= point_xc;
                                quality_mem[point_count]  <= point_quality;
                                flags_mem[point_count]    <= point_flags[14:0];
                                good_mem[point_count]     <= point_good;
                                count_next = point_count + 1'b1;
                                if (point_good)
                                    valid_count_next = valid_count + 1'b1;
                                if (point_flags[15])
                                    error <= 1'b1;
                            end else begin
                                error <= 1'b1;
                            end
                        end
                        if (finish) begin
                            point_count <= count_next;
                            valid_count <= valid_count_next;
                            state       <= SEND;
                            header_index <= 4'd0;
                            read_index   <= '0;
                            point_phase  <= 2'd0;
                            in_points    <= 1'b0;
                        end else begin
                            point_count <= count_next;
                            valid_count <= valid_count_next;
                        end
                    end
                    SEND: begin
                        if (tvalid && tready) begin
                            if (!in_points && (header_index == 4'd7)) begin
                                if (point_count == 0) begin
                                    state <= IDLE;
                                    done  <= 1'b1;
                                end else begin
                                    in_points  <= 1'b1;
                                    read_index <= '0;
                                    point_phase <= 2'd0;
                                end
                            end else if (tlast) begin
                                state <= IDLE;
                                done  <= 1'b1;
                            end else if (header_index < 4'd7) begin
                                header_index <= header_index + 1'b1;
                            end else if (point_phase == 2'd2) begin
                                point_phase <= 2'd0;
                                read_index  <= read_index + 1'b1;
                            end else begin
                                point_phase <= point_phase + 1'b1;
                            end
                        end
                    end
                    default: state <= IDLE;
                endcase
            end
        end
    end
endmodule
