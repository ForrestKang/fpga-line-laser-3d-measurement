`timescale 1ns/1ps
// Continuous fixed-point normal from one Hessian power iteration, followed by
// Newton projection delta=-(g.n)n/(n.H.n). This is not an exact eigen solver.
// One request at a time. Each accepted offset is bounded to +/-0.5 pixel.
module direction_refiner(
 input wire clk,rst,clear,start,
 input wire signed [11:0] gx,gy,hxx,hxy,hyy,
 output wire ready,
 output reg done,center_valid,
 output reg signed [12:0] du_q12,dv_q12,
 output reg signed [11:0] normal_x,normal_y
);
 localparam IDLE=0,P0=1,P1=2,NORM=3,P2=4,P3=5,P4=6,P5=7,CHECK=8,DIV=9,ABS=10,MAX=11,SHIFT=12,SELECT=13,MAG=14,SETUP=15,CHECK_RESULT=16;
 reg [4:0] state;
 reg signed [11:0] x,y,xx,xy,yy,n0x,n0y;
 (* use_dsp="yes" *) reg signed [23:0] a0,a1,a2,a3;
 reg signed [24:0] px,py;
 (* use_dsp="yes" *) reg signed [23:0] b0,b1,b2,b3,b4,b5;
 reg signed [24:0] gn,hnx,hny;
 (* use_dsp="yes" *) reg signed [36:0] c0,c1,c2,c3;
 reg signed [37:0] denominator,numerator_x,numerator_y;
 reg [49:0] rx,ry,divisor;
 reg [11:0] qx,qy;
 reg sx,sy;
 reg [3:0] step;
 integer i;
 // Keep the magnitude, maximum, priority encoder and barrel shifter in
 // separate cycles. Full01 routed px/py -> all four operations -> DSP A/B
 // input registers as a single 10.960 ns path at the 100 MHz requirement.
 (* dont_touch="yes" *) reg [4:0] shift;
 (* dont_touch="yes" *) reg [24:0] largest,ax,ay;
 reg [37:0] ad,anx,anyv;
 reg negative_denominator,reject_center;
 reg sign_x,sign_y;
 wire [49:0] dx=rx-divisor,dy=ry-divisor;
 wire [11:0] qxn=(qx<<1)|(rx>=divisor),qyn=(qy<<1)|(ry>=divisor);
 wire [12:0] abs_xx=xx<0?-$signed({xx[11],xx}):$signed({xx[11],xx});
 wire [12:0] abs_yy=yy<0?-$signed({yy[11],yy}):$signed({yy[11],yy});
 assign ready=state==IDLE;
 always @(posedge clk)begin
  done<=0;
  if(rst||clear)begin
   state<=IDLE;done<=0;center_valid<=0;du_q12<=0;dv_q12<=0;normal_x<=0;normal_y<=0;
   x<=0;y<=0;xx<=0;xy<=0;yy<=0;n0x<=0;n0y<=0;
   a0<=0;a1<=0;a2<=0;a3<=0;px<=0;py<=0;
   ax<=0;ay<=0;largest<=0;shift<=0;
   b0<=0;b1<=0;b2<=0;b3<=0;b4<=0;b5<=0;gn<=0;hnx<=0;hny<=0;
   c0<=0;c1<=0;c2<=0;c3<=0;denominator<=0;numerator_x<=0;numerator_y<=0;
   ad<=0;anx<=0;anyv<=0;negative_denominator<=0;reject_center<=0;sign_x<=0;sign_y<=0;
   rx<=0;ry<=0;divisor<=0;qx<=0;qy<=0;sx<=0;sy<=0;step<=0;
  end else case(state)
   IDLE:if(start)begin
    x<=gx;y<=gy;xx<=hxx;xy<=hxy;yy<=hyy;center_valid<=0;du_q12<=0;dv_q12<=0;
    state<=SELECT;
   end
   SELECT:begin
    if(abs_xx>=abs_yy)begin n0x<=xx;n0y<=xy;end
    else begin n0x<=xy;n0y<=yy;end
    state<=P0;
   end
   P0:begin a0<=xx*n0x;a1<=xy*n0y;a2<=xy*n0x;a3<=yy*n0y;state<=P1;end
   P1:begin px<=$signed({a0[23],a0})+$signed({a1[23],a1});py<=$signed({a2[23],a2})+$signed({a3[23],a3});state<=ABS;end
   ABS:begin ax<=px<0?-px:px;ay<=py<0?-py:py;state<=MAX;end
   MAX:begin largest<=ax>ay?ax:ay;state<=SHIFT;end
   SHIFT:begin
    shift<=0;
    for(i=10;i<25;i=i+1)if(largest[i])shift<=i-9;
    state<=NORM;
   end
   NORM:begin normal_x<=px>>>shift;normal_y<=py>>>shift;state<=P2;end
   P2:begin b0<=x*normal_x;b1<=y*normal_y;b2<=xx*normal_x;b3<=xy*normal_y;b4<=xy*normal_x;b5<=yy*normal_y;state<=P3;end
   P3:begin
    gn<=$signed({b0[23],b0})+$signed({b1[23],b1});
    hnx<=$signed({b2[23],b2})+$signed({b3[23],b3});hny<=$signed({b4[23],b4})+$signed({b5[23],b5});state<=P4;
   end
   P4:begin c0<=gn*normal_x;c1<=gn*normal_y;c2<=hnx*normal_x;c3<=hny*normal_y;state<=P5;end
   P5:begin
    numerator_x<=-$signed({c0[36],c0});numerator_y<=-$signed({c1[36],c1});
    denominator<=$signed({c2[36],c2})+$signed({c3[36],c3});state<=MAG;
   end
   MAG:begin
    ad<=denominator<0?-denominator:denominator;
    anx<=numerator_x<0?-numerator_x:numerator_x;
    anyv<=numerator_y<0?-numerator_y:numerator_y;
    negative_denominator<=denominator[37];
    sign_x<=numerator_x[37]^denominator[37];sign_y<=numerator_y[37]^denominator[37];
    state<=CHECK;
   end
   CHECK:begin
    reject_center<=!negative_denominator||ad==0||{1'b0,anx,1'b0}>{2'b0,ad}||{1'b0,anyv,1'b0}>{2'b0,ad};
    state<=CHECK_RESULT;
   end
   CHECK_RESULT:begin
    if(reject_center)begin done<=1;center_valid<=0;state<=IDLE;end
    else state<=SETUP;
   end
   SETUP:begin
     rx<={anx,12'b0};ry<={anyv,12'b0};divisor<={1'b0,ad,11'b0};
     sx<=sign_x;sy<=sign_y;
     qx<=0;qy<=0;step<=0;state<=DIV;
   end
   DIV:begin
    if(rx>=divisor)rx<=dx;if(ry>=divisor)ry<=dy;
    qx<=qxn;qy<=qyn;divisor<=divisor>>1;
    if(step==11)begin
     du_q12<=sx?-$signed({1'b0,qxn}):$signed({1'b0,qxn});
     dv_q12<=sy?-$signed({1'b0,qyn}):$signed({1'b0,qyn});
     center_valid<=1;done<=1;state<=IDLE;
    end else step<=step+1;
   end
   default:state<=IDLE;
  endcase
 end
endmodule
