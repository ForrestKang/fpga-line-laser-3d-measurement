`timescale 1ns/1ps
// R33 single shared 3x3 raw window. All derivative planes refer to one center.
// No reflected/replicated pixels are accepted as supported measurements.
module r33_window_filter #(parameter W=752, H=480)(
 input wire clk, rst, clear,
 input wire [7:0] pixel_data,
 input wire pixel_valid,
 output wire pixel_ready,
 output reg feat_valid,
 input wire feat_ready,
 output reg [9:0] feat_x,
 output reg [8:0] feat_y,
 output reg [7:0] feat_raw,
 output reg [11:0] feat_smooth_q4,
 output reg signed [11:0] feat_gx_q2, feat_gy_q2,
 output reg signed [11:0] feat_hxx_q2, feat_hxy_q2, feat_hyy_q2,
 output reg [3:0] feat_sat_count,
 output reg feat_border,
 output reg finished
);
 (* ram_style="block" *) reg [7:0] line_even[0:W-1];
 (* ram_style="block" *) reg [7:0] line_odd[0:W-1];
 reg running;
 reg [10:0] in_x;
 reg [9:0] in_y;
 reg q_valid;
 reg [10:0] q_x;
 reg [9:0] q_y;
 reg [7:0] q_even,q_odd,q_bot;
 wire [7:0] q_top=q_x==W ? 8'b0 : (q_y[0] ? q_odd : q_even);
 wire [7:0] q_mid=q_x==W ? 8'b0 : (q_y[0] ? q_even : q_odd);
 reg [7:0] t0,t1,m0,m1,b0,b1;
 wire advance=!feat_valid || feat_ready;
 wire real_position=in_x<W && in_y<H;
 wire take=running && (!real_position || pixel_valid);
 assign pixel_ready=running && real_position && advance && !rst && !clear;
 // Two read-first banks, alternate rows. Cross-writing one RAM directly from
 // another RAM's asynchronous output prevents BRAM inference; these explicit
 // synchronous read ports instead return both old rows before this row writes.
 always @(posedge clk) if(!rst && !clear && advance && take && in_x<W)begin
  q_even<=line_even[in_x];
  if(in_y<H && !in_y[0])line_even[in_x]<=pixel_data;
 end
 always @(posedge clk) if(!rst && !clear && advance && take && in_x<W)begin
  q_odd<=line_odd[in_x];
  if(in_y<H && in_y[0])line_odd[in_x]<=pixel_data;
 end

 // Read data is first captured as a patch. This removes the inferred BRAM
 // output/mux from the arithmetic path. A common clock-enable freezes every
 // stage together, including the line RAM address and horizontal history.
 reg p_valid,r_valid,d_valid;
 reg [9:0] p_x,r_x,d_x;
 reg [8:0] p_y,r_y,d_y;
 reg p_border,r_border,d_border;
 reg [7:0] w00,w01,w02,w10,w11,w12,w20,w21,w22;
 reg [7:0] r_raw,d_raw;
 wire signed [12:0] a00=$signed({5'b0,w00}),a01=$signed({5'b0,w01}),a02=$signed({5'b0,w02});
 wire signed [12:0] a10=$signed({5'b0,w10}),a11=$signed({5'b0,w11}),a12=$signed({5'b0,w12});
 wire signed [12:0] a20=$signed({5'b0,w20}),a21=$signed({5'b0,w21}),a22=$signed({5'b0,w22});
 // Every row term fits signed 13 bits: smoothing 0..1020, derivative
 // -510..510. Final smoothing 0..4080 and Hessian -2040..2040 also fit.
 reg signed [12:0] r_s0,r_s1,r_s2,r_dx0,r_dx1,r_dx2,r_dd0,r_dd1,r_dd2;
 reg [3:0] r_sat,d_sat;
 reg [11:0] d_smooth;
 reg signed [11:0] d_gx,d_gy,d_hxx,d_hxy,d_hyy;
 // Sobel sums are bounded -1020..1020. Arithmetic shift rounds negative
 // odd values away from zero; only positive odd values need the extra one.
 function automatic signed [11:0] half_away(input signed [11:0] value);
 begin half_away=(value>>>1)+$signed({11'b0,(!value[11] && value[0])});end
 endfunction

 always @(posedge clk) begin
  if(rst || clear) begin
   running<=1;in_x<=0;in_y<=0;q_valid<=0;q_x<=0;q_y<=0;
   q_bot<=0;t0<=0;t1<=0;m0<=0;m1<=0;b0<=0;b1<=0;
   p_valid<=0;r_valid<=0;d_valid<=0;
   p_x<=0;r_x<=0;d_x<=0;p_y<=0;r_y<=0;d_y<=0;
   p_border<=1;r_border<=1;d_border<=1;
   w00<=0;w01<=0;w02<=0;w10<=0;w11<=0;w12<=0;w20<=0;w21<=0;w22<=0;
   r_raw<=0;d_raw<=0;r_s0<=0;r_s1<=0;r_s2<=0;
   r_dx0<=0;r_dx1<=0;r_dx2<=0;r_dd0<=0;r_dd1<=0;r_dd2<=0;
   r_sat<=0;d_sat<=0;d_smooth<=0;d_gx<=0;d_gy<=0;d_hxx<=0;d_hxy<=0;d_hyy<=0;
   feat_valid<=0;feat_x<=0;feat_y<=0;feat_raw<=0;feat_smooth_q4<=0;
   feat_gx_q2<=0;feat_gy_q2<=0;feat_hxx_q2<=0;feat_hxy_q2<=0;feat_hyy_q2<=0;
   feat_sat_count<=0;feat_border<=1;finished<=0;
  end else if(advance) begin
   if(feat_valid && feat_ready && feat_x==W-1 && feat_y==H-1) finished<=1;
   feat_valid<=d_valid;
   if(d_valid)begin
    feat_x<=d_x;feat_y<=d_y;feat_raw<=d_raw;feat_border<=d_border;
    feat_smooth_q4<=d_smooth;feat_gx_q2<=half_away(d_gx);feat_gy_q2<=half_away(d_gy);
    feat_hxx_q2<=d_hxx;feat_hxy_q2<=d_hxy;feat_hyy_q2<=d_hyy;feat_sat_count<=d_sat;
   end
   d_valid<=r_valid;
   if(r_valid)begin
    d_x<=r_x;d_y<=r_y;d_raw<=r_raw;d_border<=r_border;d_sat<=r_sat;
    d_smooth<=r_s0+(r_s1<<<1)+r_s2;
    d_gx<=r_dx0+(r_dx1<<<1)+r_dx2;
    d_gy<=r_s2-r_s0;
    d_hxx<=r_dd0+(r_dd1<<<1)+r_dd2;
    d_hyy<=r_s0-(r_s1<<<1)+r_s2;
    d_hxy<=r_dx2-r_dx0;
   end
   r_valid<=p_valid;
   if(p_valid)begin
    r_x<=p_x;r_y<=p_y;r_raw<=w11;r_border<=p_border;
    r_s0<=a00+(a01<<<1)+a02;r_s1<=a10+(a11<<<1)+a12;r_s2<=a20+(a21<<<1)+a22;
    r_dx0<=a02-a00;r_dx1<=a12-a10;r_dx2<=a22-a20;
    r_dd0<=a00-(a01<<<1)+a02;r_dd1<=a10-(a11<<<1)+a12;r_dd2<=a20-(a21<<<1)+a22;
    r_sat<={3'b0,(w00>=250)}+{3'b0,(w01>=250)}+{3'b0,(w02>=250)}+
           {3'b0,(w10>=250)}+{3'b0,(w11>=250)}+{3'b0,(w12>=250)}+
           {3'b0,(w20>=250)}+{3'b0,(w21>=250)}+{3'b0,(w22>=250)};
   end
   p_valid<=q_valid && q_x>0 && q_y>0;
   if(q_valid) begin
    if(q_x==0) begin t0<=0;m0<=0;b0<=0;end
    else begin t0<=t1;m0<=m1;b0<=b1;end
    t1<=q_top;m1<=q_mid;b1<=q_bot;
    if(q_x>0 && q_y>0) begin
     p_x<=q_x-1;p_y<=q_y-1;
     w00<=t0;w01<=t1;w02<=q_top;w10<=m0;w11<=m1;w12<=q_mid;w20<=b0;w21<=b1;w22<=q_bot;
     p_border<=q_x==1 || q_x==W || q_y==1 || q_y==H;
    end
   end
   q_valid<=take;
   if(take) begin
    q_x<=in_x;q_y<=in_y;
    if(in_x<W) begin
     q_bot<=in_y<H ? pixel_data : 8'b0;
    end else q_bot<=0;
    if(in_x==W) begin
     in_x<=0;
     if(in_y==H)running<=0;else in_y<=in_y+1;
    end else in_x<=in_x+1;
   end
  end
 end
endmodule
