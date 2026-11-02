`timescale 1ns/1ps
// FWFT row-event crossing. USE_XPM=0 is a digital Gray-pointer model, not
// vendor/static CDC sign-off. Reset release waits for both running clocks.
module row_transaction_cdc #(
    parameter DATA_WIDTH=256, parameter DEPTH=1024, parameter USE_XPM=0,
    parameter RESET_HOLD=64
)(
    input wire reset, input wire wr_clk, input wire rd_clk,
    input wire wr_valid, input wire [DATA_WIDTH-1:0] wr_data,
    output wire wr_ready, output wire full, output wire almost_full,
    input wire rd_ready, output wire rd_valid,
    output wire [DATA_WIDTH-1:0] rd_data,
    output wire wr_busy, output wire rd_busy,
    output wire overflow
);
    localparam AW=$clog2(DEPTH);
    reg [RESET_HOLD-1:0] reset_hold = {RESET_HOLD{1'b1}};
    always @(posedge wr_clk or posedge reset)
        if (reset) reset_hold <= {RESET_HOLD{1'b1}};
        else reset_hold <= {reset_hold[RESET_HOLD-2:0],1'b0};
    wire fifo_reset=reset_hold[RESET_HOLD-1];
    wire empty;
    assign wr_ready=!full && !wr_busy && !reset;
    assign rd_valid=!empty && !rd_busy && !reset;
    generate if (USE_XPM) begin : vendor
        wire underflow;
        wire primitive_wr_busy,primitive_rd_busy;
        // XPM busy flags cannot react to a reset until a write clock edge.
        // Include the coordinator's held reset, otherwise a stopped camera
        // could falsely advertise readiness after the system flush expires.
        assign wr_busy=fifo_reset|primitive_wr_busy;
        assign rd_busy=fifo_reset|primitive_rd_busy;
        xpm_fifo_async #(
            .FIFO_MEMORY_TYPE("auto"), .ECC_MODE("no_ecc"),
            .RELATED_CLOCKS(0), .SIM_ASSERT_CHK(1),
            .FIFO_WRITE_DEPTH(DEPTH), .WRITE_DATA_WIDTH(DATA_WIDTH),
            .READ_DATA_WIDTH(DATA_WIDTH), .READ_MODE("fwft"),
            .FIFO_READ_LATENCY(0), .CDC_SYNC_STAGES(2),
            .PROG_FULL_THRESH(DEPTH-8), .PROG_EMPTY_THRESH(8),
            .WR_DATA_COUNT_WIDTH(AW+1), .RD_DATA_COUNT_WIDTH(AW+1),
            .USE_ADV_FEATURES("0707")
        ) fifo (
            .rst(fifo_reset),.sleep(1'b0),.wr_clk(wr_clk),.rd_clk(rd_clk),
            .din(wr_data),.wr_en(wr_valid && wr_ready),.rd_en(rd_ready && rd_valid),
            .dout(rd_data),.full(full),.empty(empty),.prog_full(almost_full),
            .wr_rst_busy(primitive_wr_busy),.rd_rst_busy(primitive_rd_busy),
            .overflow(),.underflow(underflow),.injectsbiterr(1'b0),.injectdbiterr(1'b0),
            .sbiterr(),.dbiterr(),.almost_full(),.almost_empty(),
            .wr_ack(),.data_valid(),.wr_data_count(),.rd_data_count(),.prog_empty()
        );
    end else begin : model
        reg [DATA_WIDTH-1:0] mem[0:DEPTH-1];
        reg [AW:0] wb=0,rb=0,wg=0,rg=0;
        (* ASYNC_REG="TRUE" *) reg [AW:0] rs1=0,rs2=0,ws1=0,ws2=0;
        (* ASYNC_REG="TRUE" *) reg [3:0] rd_release=4'b1111;
        wire [AW:0] wb_next=wb+1'b1, rb_next=rb+1'b1;
        function [AW:0] ungray(input [AW:0] g);
            integer i; begin ungray[AW]=g[AW];
                for(i=AW-1;i>=0;i=i-1) ungray[i]=ungray[i+1]^g[i]; end
        endfunction
        assign full=(wg == {~rs2[AW:AW-1],rs2[AW-2:0]});
        assign almost_full=((wb-ungray(rs2)) >= DEPTH-8);
        assign empty=(rg==ws2);
        assign wr_busy=fifo_reset;
        assign rd_busy=|rd_release;
        assign rd_data=mem[rb[AW-1:0]];
        always @(posedge wr_clk or posedge fifo_reset) begin
            if(fifo_reset) begin wb<=0;wg<=0;rs1<=0;rs2<=0; end
            else begin
                rs1<=rg;rs2<=rs1;
                if(wr_valid && wr_ready) begin
                    mem[wb[AW-1:0]]<=wr_data;wb<=wb_next;wg<=(wb_next>>1)^wb_next;
                end
            end
        end
        always @(posedge rd_clk or posedge fifo_reset) begin
            if(fifo_reset) begin rb<=0;rg<=0;ws1<=0;ws2<=0;rd_release<=4'b1111; end
            else begin
                rd_release<={rd_release[2:0],1'b0};ws1<=wg;ws2<=ws1;
                if(rd_ready && rd_valid) begin rb<=rb_next;rg<=(rb_next>>1)^rb_next; end
            end
        end
    end endgenerate
    // Rejected attempted writes are explicit even though the primitive is gated.
    reg overflow_reg=0;
    always @(posedge wr_clk or posedge reset)
        if(reset) overflow_reg<=0;
        else overflow_reg<=wr_valid && !wr_ready && !wr_busy;
    assign overflow=overflow_reg;
endmodule
