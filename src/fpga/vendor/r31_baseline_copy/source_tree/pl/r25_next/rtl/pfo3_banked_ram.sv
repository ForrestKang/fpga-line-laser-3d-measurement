`timescale 1ns/1ps
// Four interleaved banks: A is one arbitrary node, B is either one arbitrary
// node or four aligned consecutive nodes. All reads retain one-cycle latency.
// During pair processing A is in row y-1, B is in row y, so same-address dual
// writes cannot occur. Initialization and later label/output stages use B1.
module pfo3_banked_ram #(parameter WIDTH=16,DEPTH=7680)(
 input wire clk,
 input wire [12:0] aa,ab,
 input wire wa,
 input wire [WIDTH-1:0] da,
 input wire b4,
 input wire [3:0] wb,
 input wire [4*WIDTH-1:0] db,
 output wire [WIDTH-1:0] qa,qb,
 output wire [4*WIDTH-1:0] qb4
);
 wire [4*WIDTH-1:0] aq;
 reg [1:0] a_bank,b_bank;
 always @(posedge clk)begin a_bank<=aa[1:0];b_bank<=ab[1:0];end
 assign qa=aq[a_bank*WIDTH+:WIDTH];
 assign qb=qb4[b_bank*WIDTH+:WIDTH];
 genvar g;
 generate for(g=0;g<4;g=g+1)begin:bank
  pfo3_tdp_ram #(.WIDTH(WIDTH),.DEPTH((DEPTH+3)/4)) ram(
   .clk(clk),.aa({2'b0,aa[12:2]}),.ab({2'b0,ab[12:2]}),
   .wa(wa&&aa[1:0]==g),.wb(wb[g]&&(b4||ab[1:0]==g)),
   .da(da),.db(db[g*WIDTH+:WIDTH]),
   .qa(aq[g*WIDTH+:WIDTH]),.qb(qb4[g*WIDTH+:WIDTH]));
 end endgenerate
endmodule
