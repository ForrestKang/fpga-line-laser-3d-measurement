`timescale 1ns/1ps
`default_nettype none
// Two 1024-point banks form one bounded FIFO. Occupancy owns storage until the
// output handshake; neither pointer may advance on a stalled transfer.
module point_cloud_cache #(parameter ADDR_BITS=11)(
 input wire clk,rst,clear,
 input wire [255:0] in_data,input wire in_valid,output wire in_ready,
 output reg [255:0] out_data,output reg out_valid,input wire out_ready,
 output reg [ADDR_BITS:0] occupancy,output reg [ADDR_BITS:0] high_water
);
 localparam DEPTH=1<<ADDR_BITS;
 (* ram_style="block" *) reg [255:0] mem[0:DEPTH-1];
 reg [ADDR_BITS-1:0] wr,rd;
 wire pop=out_valid&&out_ready;
 wire load=(!out_valid||out_ready)&&(occupancy!=0);
 wire push=in_valid&&in_ready;
 // Occupancy counts RAM-owned entries. The prefetched output register is a
// separate owned entry; total capacity is DEPTH, including that register.
 assign in_ready=(occupancy+out_valid)<DEPTH;
 always @(posedge clk)begin
  if(rst||clear)begin wr<=0;rd<=0;out_valid<=0;out_data<=0;occupancy<=0;high_water<=0;end
  else begin
   if(push)begin mem[wr]<=in_data;wr<=wr+1'b1;end
   if(load)begin out_data<=mem[rd];rd<=rd+1'b1;out_valid<=1;end
   else if(pop)out_valid<=0;
   case({push,load})2'b10:occupancy<=occupancy+1'b1;2'b01:occupancy<=occupancy-1'b1;default:begin end endcase
   if(occupancy+out_valid>high_water)high_water<=occupancy+out_valid;
  end
 end
endmodule
`default_nettype wire
