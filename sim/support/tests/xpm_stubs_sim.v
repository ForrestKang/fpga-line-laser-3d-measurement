`timescale 1ns/1ps
// SIMULATION ONLY behavioural stand-ins for the two Xilinx XPM CDC macros used by
// r40_cloud_view (Icarus cannot compile the vendor sources). They follow the
// documented protocol; metastability and CDC timing are covered by Vivado report_cdc.
module xpm_cdc_single #(parameter DEST_SYNC_FF=4,INIT_SYNC_FF=0,SIM_ASSERT_CHK=0,SRC_INPUT_REG=1)(
 input wire src_clk,input wire src_in,input wire dest_clk,output wire dest_out);
 reg s=0;reg [DEST_SYNC_FF-1:0] d=0;
 always @(posedge src_clk)s<=src_in;
 wire src=SRC_INPUT_REG?s:src_in;
 always @(posedge dest_clk)d<={d[DEST_SYNC_FF-2:0],src};
 assign dest_out=d[DEST_SYNC_FF-1];
endmodule

// DEST_EXT_HSK=0 only: dest_req pulses one dest_clk cycle when dest_out is valid;
// src_rcv follows the synchronized request level back into the source domain.
module xpm_cdc_handshake #(parameter DEST_EXT_HSK=0,DEST_SYNC_FF=4,INIT_SYNC_FF=0,SIM_ASSERT_CHK=0,
 SRC_SYNC_FF=4,WIDTH=1)(
 input wire src_clk,input wire [WIDTH-1:0] src_in,input wire src_send,output wire src_rcv,
 input wire dest_clk,output reg [WIDTH-1:0] dest_out,output reg dest_req,input wire dest_ack);
 reg send_q=0;reg [WIDTH-1:0] data_q=0;
 always @(posedge src_clk)begin
  if(src_send&&!send_q)data_q<=src_in;
  send_q<=src_send;
 end
 reg [DEST_SYNC_FF-1:0] rs=0;reg rprev=0;
 initial begin dest_out=0;dest_req=0;end
 always @(posedge dest_clk)begin
  rs<={rs[DEST_SYNC_FF-2:0],send_q};rprev<=rs[DEST_SYNC_FF-1];
  dest_req<=rs[DEST_SYNC_FF-1]&&!rprev;
  if(rs[DEST_SYNC_FF-1]&&!rprev)dest_out<=data_q;
 end
 reg [SRC_SYNC_FF-1:0] as=0;
 always @(posedge src_clk)as<={as[SRC_SYNC_FF-2:0],rprev};
 assign src_rcv=as[SRC_SYNC_FF-1];
endmodule
