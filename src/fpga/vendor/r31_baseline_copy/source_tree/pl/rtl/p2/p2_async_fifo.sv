`timescale 1ns/1ps

// P2-controlled asynchronous stream FIFO.
//
// The P1 adapter deliberately has one reset input because it is also used by
// the frozen baseline.  The P2 capture core has two reset domains: the
// camera-side reset is released only by a local cam_pclk synchronizer, while
// the system-side flush must become visible immediately to AXI/DMA logic.
// This copy keeps those release paths separate and is used only by P2.  The
// USE_XPM=1 implementation maps to AMD's xpm_fifo_async; USE_XPM=0 is a
// deterministic Gray-pointer model for Icarus and unit tests.
module p2_async_fifo #(
    parameter integer DATA_WIDTH = 256,
    parameter integer DEPTH = 1024,
    parameter integer USE_XPM = 0,
    parameter integer RESET_HOLD = 4
) (
    input  wire reset_wr,
    input  wire reset_rd,
    input  wire wr_clk,
    input  wire rd_clk,
    input  wire wr_valid,
    input  wire [DATA_WIDTH-1:0] wr_data,
    output wire wr_ready,
    output wire full,
    output wire almost_full,
    input  wire rd_ready,
    output wire rd_valid,
    output wire [DATA_WIDTH-1:0] rd_data,
    output wire wr_busy,
    output wire rd_busy,
    output wire overflow
);
    localparam integer AW = $clog2(DEPTH);

    generate
        if (USE_XPM) begin : vendor
            wire primitive_wr_busy;
            wire primitive_rd_busy;
            wire underflow;
            // UG974 requires xpm_fifo_async.rst to be synchronous to wr_clk.
            // reset_wr is already the camera-domain reset barrier, but its
            // asynchronous assertion is converted to a registered level at
            // the FIFO write clock.  Four asserted cycles also give the
            // primitive a deterministic reset pulse after a stopped camera
            // clock restarts.  reset_rd is deliberately *not* ORed into the
            // XPM reset: the system side gates reads and reports rd_busy
            // during an abort, while the write-domain reset flushes both XPM
            // pointers when the camera clock is available again.
            (* ASYNC_REG = "TRUE", SHREG_EXTRACT = "NO" *)
            // Give the XPM reset input a known FPGA power-up value.  Without
            // an INIT value, the first camera clock edge can sample X on the
            // synchronous XPM rst input before this pipeline has evaluated
            // reset_wr.  The declaration initializer maps to the FDRE INIT
            // attribute in the Xilinx implementation and is also harmless
            // for the Icarus model.
            reg [3:0] wr_reset_pipe = 4'b1111;
            always @(posedge wr_clk) begin
                if (reset_wr)
                    wr_reset_pipe <= 4'b1111;
                else
                    wr_reset_pipe <= {wr_reset_pipe[2:0], 1'b0};
            end
            wire fifo_reset = wr_reset_pipe[3];
            assign wr_busy = reset_wr | fifo_reset | primitive_wr_busy;
            // Do not fan the camera-domain fifo_reset directly into the
            // system-domain ready/AXI gating.  The primitive's rd_rst_busy
            // is already synchronized into rd_clk; cam_domain_ready and the
            // write-busy synchronizer hold the core unavailable until that
            // barrier has settled.
            assign rd_busy = reset_rd | primitive_rd_busy;
            assign wr_ready = !full && !wr_busy && !reset_wr;
            assign rd_valid = !empty && !rd_busy && !reset_rd;
            wire empty;
            xpm_fifo_async #(
                .FIFO_MEMORY_TYPE("auto"),
                .ECC_MODE("no_ecc"),
                .RELATED_CLOCKS(0),
                .SIM_ASSERT_CHK(1),
                .FIFO_WRITE_DEPTH(DEPTH),
                .WRITE_DATA_WIDTH(DATA_WIDTH),
                .READ_DATA_WIDTH(DATA_WIDTH),
                .READ_MODE("fwft"),
                .FIFO_READ_LATENCY(0),
                .CDC_SYNC_STAGES(2),
                .PROG_FULL_THRESH(DEPTH-8),
                .PROG_EMPTY_THRESH(8),
                .WR_DATA_COUNT_WIDTH(AW+1),
                .RD_DATA_COUNT_WIDTH(AW+1),
                .USE_ADV_FEATURES("0707")
            ) fifo (
                .rst(fifo_reset),
                .sleep(1'b0),
                .wr_clk(wr_clk),
                .rd_clk(rd_clk),
                .din(wr_data),
                .wr_en(wr_valid && wr_ready),
                .rd_en(rd_ready && rd_valid),
                .dout(rd_data),
                .full(full),
                .empty(empty),
                .prog_full(almost_full),
                .wr_rst_busy(primitive_wr_busy),
                .rd_rst_busy(primitive_rd_busy),
                .overflow(),
                .underflow(underflow),
                .injectsbiterr(1'b0),
                .injectdbiterr(1'b0),
                .sbiterr(),
                .dbiterr(),
                .almost_full(),
                .almost_empty(),
                .wr_ack(),
                .data_valid(),
                .wr_data_count(),
                .rd_data_count(),
                .prog_empty()
            );
            reg overflow_reg = 1'b0;
            always @(posedge wr_clk or posedge reset_wr) begin
                if (reset_wr)
                    overflow_reg <= 1'b0;
                else
                    overflow_reg <= wr_valid && !wr_ready && !wr_busy;
            end
            assign overflow = overflow_reg;
        end else begin : model
            // Cross-domain reset requests are sampled through local
            // synchronizers before they are used as pointer resets.  This
            // prevents a system flush from becoming a direct reset/release
            // path on camera-domain storage elements.
            (* ASYNC_REG = "TRUE" *) reg rd_reset_w1, rd_reset_w2;
            (* ASYNC_REG = "TRUE" *) reg wr_reset_r1, wr_reset_r2;
            (* ASYNC_REG = "TRUE" *) reg [RESET_HOLD-1:0] wr_release;
            (* ASYNC_REG = "TRUE" *) reg [RESET_HOLD-1:0] rd_release;
            reg [DATA_WIDTH-1:0] mem [0:DEPTH-1];
            reg [AW:0] wr_bin, rd_bin, wr_gray, rd_gray;
            (* ASYNC_REG = "TRUE" *) reg [AW:0] rd_gray_w1, rd_gray_w2;
            (* ASYNC_REG = "TRUE" *) reg [AW:0] wr_gray_r1, wr_gray_r2;
            reg overflow_reg;

            wire wr_fifo_reset = reset_wr | rd_reset_w2;
            wire rd_fifo_reset = reset_rd | wr_reset_r2;
            wire [AW:0] wr_bin_next = wr_bin + 1'b1;
            wire [AW:0] rd_bin_next = rd_bin + 1'b1;
            wire [AW:0] rd_gray_in_wr = rd_gray_w2;
            wire [AW:0] wr_gray_in_rd = wr_gray_r2;
            wire fifo_full = (wr_gray == {~rd_gray_in_wr[AW:AW-1],
                                            rd_gray_in_wr[AW-2:0]});
            wire fifo_empty = (rd_gray == wr_gray_in_rd);

            assign full = fifo_full;
            assign almost_full = ((wr_bin - gray_to_bin(rd_gray_in_wr)) >=
                                  (DEPTH-8));
            assign wr_busy = wr_fifo_reset | (|wr_release);
            assign rd_busy = rd_fifo_reset | (|rd_release);
            assign wr_ready = !full && !wr_busy && !reset_wr;
            assign rd_valid = !fifo_empty && !rd_busy && !reset_rd;
            assign rd_data = mem[rd_bin[AW-1:0]];
            assign overflow = overflow_reg;

            function [AW:0] gray_to_bin(input [AW:0] value);
                integer index;
                begin
                    gray_to_bin[AW] = value[AW];
                    for (index = AW-1; index >= 0; index = index - 1)
                        gray_to_bin[index] = gray_to_bin[index+1] ^ value[index];
                end
            endfunction

            always @(posedge wr_clk or posedge reset_wr) begin
                if (reset_wr) begin
                    rd_reset_w1 <= 1'b1;
                    rd_reset_w2 <= 1'b1;
                    wr_release <= {RESET_HOLD{1'b1}};
                end else begin
                    rd_reset_w1 <= reset_rd;
                    rd_reset_w2 <= rd_reset_w1;
                    wr_release <= {wr_release[RESET_HOLD-2:0], 1'b0};
                end
            end

            always @(posedge rd_clk or posedge reset_rd) begin
                if (reset_rd) begin
                    wr_reset_r1 <= 1'b1;
                    wr_reset_r2 <= 1'b1;
                    rd_release <= {RESET_HOLD{1'b1}};
                end else begin
                    wr_reset_r1 <= reset_wr;
                    wr_reset_r2 <= wr_reset_r1;
                    rd_release <= {rd_release[RESET_HOLD-2:0], 1'b0};
                end
            end

            always @(posedge wr_clk or posedge wr_fifo_reset) begin
                if (wr_fifo_reset) begin
                    wr_bin <= 0;
                    wr_gray <= 0;
                    rd_gray_w1 <= 0;
                    rd_gray_w2 <= 0;
                    overflow_reg <= 1'b0;
                end else begin
                    rd_gray_w1 <= rd_gray;
                    rd_gray_w2 <= rd_gray_w1;
                    overflow_reg <= wr_valid && !wr_ready && !wr_busy;
                    if (wr_valid && wr_ready) begin
                        mem[wr_bin[AW-1:0]] <= wr_data;
                        wr_bin <= wr_bin_next;
                        wr_gray <= (wr_bin_next >> 1) ^ wr_bin_next;
                    end
                end
            end

            always @(posedge rd_clk or posedge rd_fifo_reset) begin
                if (rd_fifo_reset) begin
                    rd_bin <= 0;
                    rd_gray <= 0;
                    wr_gray_r1 <= 0;
                    wr_gray_r2 <= 0;
                end else begin
                    wr_gray_r1 <= wr_gray;
                    wr_gray_r2 <= wr_gray_r1;
                    if (rd_ready && rd_valid) begin
                        rd_bin <= rd_bin_next;
                        rd_gray <= (rd_bin_next >> 1) ^ rd_bin_next;
                    end
                end
            end
        end
    endgenerate
endmodule
