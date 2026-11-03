`timescale 1ns/1ps
// point cloud cloud-view memories, written in Vivado block-RAM inference templates.
// Read latency is fixed and part of the point_cloud_renderer contract.
// point cloud b04: every port has an enable (BRAM EN + output register CE). The caller holds
// it high for the whole read pipeline whose data it consumes, so results are unchanged;
// idle ports stop toggling to cut BRAM dynamic power.

// Voxel occupancy: port A read-only (renderer), port B read/write (commit, clear,
// readback). One clock, latency 2 on both ports (BRAM output register).
module voxel_memory #(parameter DEPTH=12288,AW=14)(
 input wire clk,
 input wire en_a,input wire [AW-1:0] addr_a,output reg [31:0] q_a,
 input wire en_b,input wire we_b,input wire [AW-1:0] addr_b,input wire [31:0] d_b,output reg [31:0] q_b
);
 (* ram_style="block" *) reg [31:0] mem[0:DEPTH-1];
 reg [31:0] a1,b1;
 integer i;
 initial for(i=0;i<DEPTH;i=i+1)mem[i]=0;
 always @(posedge clk)if(en_a)begin a1<=mem[addr_a];q_a<=a1;end
 always @(posedge clk)if(en_b)begin
  if(we_b)mem[addr_b]<=d_b;
  b1<=mem[addr_b];q_b<=b1;
 end
endmodule

// Commit staging FIFO storage: simple dual port, read latency 1.
module staging_memory #(parameter DEPTH=2048,AW=11,W=19)(
 input wire clk,input wire we,input wire [AW-1:0] waddr,input wire [W-1:0] wdata,
 input wire re,input wire [AW-1:0] raddr,output reg [W-1:0] q
);
 (* ram_style="block" *) reg [W-1:0] mem[0:DEPTH-1];
 always @(posedge clk)begin
  if(we)mem[waddr]<=wdata;
  if(re)q<=mem[raddr];
 end
endmodule

// Double-buffered 4-bit colour panel. Port A (render clock): write + readback,
// latency 2, gated by en_a. Port B (video clock): read-only, latency 2, both stages
// gated by en_b so a stalled video pipeline holds its data.
module colour_memory #(parameter DEPTH=107520,AW=17)(
 input wire clk_a,input wire en_a,input wire we_a,input wire [AW-1:0] addr_a,input wire [3:0] d_a,output reg [3:0] q_a,
 input wire clk_b,input wire en_b,input wire [AW-1:0] addr_b,output reg [3:0] q_b
);
 (* ram_style="block" *) reg [3:0] mem[0:DEPTH-1];
 reg [3:0] a1,b1;
 integer i;
 initial for(i=0;i<DEPTH;i=i+1)mem[i]=0;
 always @(posedge clk_a)if(en_a)begin
  if(we_a)mem[addr_a]<=d_a;
  a1<=mem[addr_a];q_a<=a1;
 end
 always @(posedge clk_b)begin
  if(en_b)b1<=mem[addr_b];
  if(en_b)q_b<=b1;
 end
endmodule
