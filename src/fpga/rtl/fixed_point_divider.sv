`timescale 1ns/1ps
`default_nettype none
// Signed Q30 division. Restoring division of abs(a)<<30 by abs(b).
// Fixed 96 iterations, no combinational '/', nearest-even quotient.
module fixed_point_divider(
 input wire clk,rst,start,
 input wire signed [63:0] a,b,
 output reg busy,done,overflow,zero,
 output reg signed [63:0] value
);
 reg [95:0] dividend,quotient;
 reg [64:0] remainder;
 reg [63:0] divisor;
 reg [6:0] step;
 reg negative;
 reg [1:0] finish_phase;
 reg [96:0] rounded_magnitude;
 reg signed [97:0] signed_result;
 wire [64:0] shifted={remainder[63:0],dividend[95]};
 wire take=shifted>={1'b0,divisor};
 wire [64:0] next_remainder=take?shifted-{1'b0,divisor}:shifted;
 wire [95:0] next_quotient={quotient[94:0],take};
 wire round_up=({remainder,1'b0}>{2'b0,divisor})||
  (({remainder,1'b0}=={2'b0,divisor})&&quotient[0]);
 wire [63:0] abs_a=a[63]?-a:a,abs_b=b[63]?-b:b;
 always @(posedge clk)begin
  done<=0;
  if(rst)begin busy<=0;done<=0;overflow<=0;zero<=0;value<=0;
   dividend<=0;quotient<=0;remainder<=0;divisor<=0;step<=0;negative<=0;
   finish_phase<=0;rounded_magnitude<=0;signed_result<=0;end
  else if(start&&!busy)begin
   zero<=b==0;overflow<=0;value<=0;
   if(b==0)begin done<=1;busy<=0;end
   else begin
    dividend<={2'b0,abs_a,30'b0};divisor<=abs_b;quotient<=0;remainder<=0;
    negative<=a[63]^b[63];step<=0;busy<=1;finish_phase<=0;
   end
  end else if(busy)begin
   // Last restore, rounding, sign application, and overflow are deliberately
   // separate cycles. Chaining all four created a 47-level 100MHz violation.
   if(finish_phase==0)begin
    dividend<=dividend<<1;quotient<=next_quotient;remainder<=next_remainder;
    if(step==95)finish_phase<=1;else step<=step+1;
   end else if(finish_phase==1)begin
    rounded_magnitude<={1'b0,quotient}+round_up;finish_phase<=2;
   end else if(finish_phase==2)begin
    signed_result<=negative?-$signed({1'b0,rounded_magnitude}):$signed({1'b0,rounded_magnitude});
    finish_phase<=3;
   end else begin
    value<=signed_result[63:0];overflow<=signed_result!=$signed(signed_result[63:0]);
    busy<=0;done<=1;finish_phase<=0;
   end
  end
 end
endmodule
`default_nettype wire
