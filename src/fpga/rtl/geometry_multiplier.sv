`timescale 1ns/1ps
`default_nettype none
// Single registered signed multiplier shared by the geometry microsequencer.
// Q30 output is rounded to nearest even; raw product is retained for gates.
module geometry_multiplier(
 input wire clk,rst,start,
 input wire signed [63:0] a,b,
 output reg done,overflow,
 output reg signed [63:0] value,
 output reg signed [127:0] raw
);
 (* use_dsp="yes" *) reg signed [127:0] product;
 reg [2:0] valid;
 reg signed [127:0] rounded;
 wire [29:0] fraction=product[29:0];
 wire increment=(fraction>30'h20000000)||((fraction==30'h20000000)&&product[30]);
 always @(posedge clk)begin
  if(rst)begin product<=0;valid<=0;rounded<=0;value<=0;raw<=0;done<=0;overflow<=0;end
  else begin
   valid<={valid[1:0],start};done<=valid[1];
   if(start)product<=a*b;
   if(valid[0])rounded<=$signed(product>>>30)+$signed({127'b0,increment});
   if(valid[1])begin
    value<=rounded[63:0];overflow<=rounded!=$signed(rounded[63:0]);raw<=product;
   end
  end
 end
endmodule
`default_nettype wire
