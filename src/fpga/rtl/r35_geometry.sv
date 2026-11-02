`timescale 1ns/1ps
`default_nettype none
// Registered, shared-DSP Q30 geometry. One point in flight; caller applies the
// final frame acceptance gate. Brown inversion uses five bounded iterations.
module r35_geometry(
 input wire clk,rst,clear,
 input wire cfg_we,input wire [6:0] cfg_addr,input wire signed [63:0] cfg_wdata,
 input wire cfg_commit,input wire [31:0] cfg_epoch,
 output reg cfg_ready,output reg cfg_error,
 input wire in_valid,output wire in_ready,
 input wire [31:0] u_q12,v_q12,candidate_id,input wire [1:0] mode_bits,
 output reg out_valid,input wire out_ready,output reg [255:0] out_record,
 output reg [31:0] out_epoch,output wire busy,output reg [31:0] point_cycles
);
 localparam signed [63:0] ONE=64'sd1073741824;
 localparam IDLE=0,WAIT_MUL=1,WAIT_DIV=2,NX=3,NY=4,NX_DONE=5,NY_DONE=6,
  B_X2=10,B_Y2=11,B_XY=12,B_R2=13,B_R4=14,B_R6=15,B_K1=16,B_K2=17,B_K3=18,
  B_DX1=19,B_DX2=20,B_DY1=21,B_DY2=22,B_IX=23,B_IY=24,B_NEXT=25,
  Q_X=30,Q_Y=31,Q_SUM=32,Q_SQUARE=33,L_X=34,L_Y=35,L_SUM=36,PARALLEL=37,PAR_CHECK=38,
  DEPTH=39,CAM_X=40,CAM_Y=41,CAM_DONE=42,R_X=43,R_Y=44,R_Z=45,R_CHECK=46,BOUNDS=47,
  ROTATE=48,ROTATE_ADD=49,ROTATE_NEXT=50,EMIT=51,OUTPUT=52;
 localparam EMIT_CHECK=55,EMIT_PACK=56,R_RANGE=57;
 reg [6:0] state,resume;
 reg signed [63:0] shadow[0:35],c[0:35];
 reg [31:0] epoch,id;
 reg [1:0] modes;
 reg [7:0] reason;
 reg [2:0] iteration;
 reg [1:0] row,col;
 reg signed [63:0] xd,yd,x,y,x2,y2,xy,r2,r4,r6,radial,dx,dy,temp,q,l2,t;
 reg signed [63:0] cam[0:2],axis[0:2];
 // Q30->Q16 floor is signed50 bits; INT64_MAX may round up to +2^49,
 // so retain signed51 until the separate signed32 overflow check.
 reg signed [50:0] camera_rounded[0:2],axis_rounded[0:2];
 reg signed [31:0] camera_q16[0:2],axis_q16[0:2];
 reg camera_overflow,axis_overflow;
 reg signed [63:0] accumulator;
 reg signed [127:0] q_squared;
 reg mul_start,div_start;
 reg signed [63:0] op_a,op_b,m;
 // b07: operand sums registered a state early so no adder feeds the DSP/divider inputs.
 reg signed [63:0] r2x2,r2y2,xdx,ydy,nc12;
 // b08: r2 is formed in B_XY (from y2 = m) and its range test registered in B_R2, so the
 // B_R4 branch that starts the multiplier no longer waits on a 64-bit compare.
 reg r2_bad;
 reg signed [127:0] full_product;
 wire mul_done,mul_overflow,div_done,div_overflow,div_zero,div_busy;
 wire signed [63:0] mul_value,div_value;
 wire signed [127:0] mul_raw;
 integer i;
 assign busy=state!=IDLE||out_valid;
 assign in_ready=state==IDLE&&!out_valid&&!cfg_commit&&!cfg_we;
 r35_geo_mul64 multiplier(.clk(clk),.rst(rst||clear),.start(mul_start),.a(op_a),.b(op_b),
  .done(mul_done),.overflow(mul_overflow),.value(mul_value),.raw(mul_raw));
 r35_div_q30 divider(.clk(clk),.rst(rst||clear),.start(div_start),.a(op_a),.b(op_b),
  .busy(div_busy),.done(div_done),.overflow(div_overflow),.zero(div_zero),.value(div_value));

 task multiply(input signed [63:0] a,input signed [63:0] b,input [6:0] next_state);
  begin op_a<=a;op_b<=b;mul_start<=1;resume<=next_state;state<=WAIT_MUL;end
 endtask
 task divide(input signed [63:0] a,input signed [63:0] b,input [6:0] next_state);
  begin op_a<=a;op_b<=b;div_start<=1;resume<=next_state;state<=WAIT_DIV;end
 endtask
 function signed [50:0] q16_round51(input signed [63:0] value_q30);
  reg signed [49:0] floor_value;
  begin floor_value=$signed(value_q30[63:14]);
   q16_round51=$signed({floor_value[49],floor_value})+
    (((value_q30[13:0]>14'h2000)||((value_q30[13:0]==14'h2000)&&floor_value[0]))?51'sd1:51'sd0);
  end
 endfunction
 always @(posedge clk)begin
  mul_start<=0;div_start<=0;cfg_error<=0;
  if(rst||clear)begin
   state<=IDLE;resume<=IDLE;cfg_ready<=0;cfg_error<=0;epoch<=0;out_epoch<=0;id<=0;modes<=0;reason<=0;
   out_valid<=0;out_record<=0;point_cycles<=0;mul_start<=0;div_start<=0;op_a<=0;op_b<=0;m<=0;full_product<=0;
   xd<=0;yd<=0;x<=0;y<=0;x2<=0;y2<=0;xy<=0;r2<=0;r4<=0;r6<=0;radial<=0;dx<=0;dy<=0;
   temp<=0;q<=0;l2<=0;t<=0;accumulator<=0;q_squared<=0;iteration<=0;row<=0;col<=0;
   for(i=0;i<36;i=i+1)begin shadow[i]<=0;c[i]<=0;end
   camera_overflow<=0;axis_overflow<=0;
   for(i=0;i<3;i=i+1)begin
    cam[i]<=0;axis[i]<=0;camera_rounded[i]<=0;axis_rounded[i]<=0;
    camera_q16[i]<=0;axis_q16[i]<=0;
   end
  end else begin
   if(cfg_we)begin if(cfg_addr<36)shadow[cfg_addr]<=cfg_wdata;else cfg_error<=1;end
   if(cfg_commit)begin
    if(busy||cfg_we||shadow[2]<=0||shadow[3]<=0||shadow[13]<=0||shadow[35]<=0||
      shadow[35]>64'sd17179869184||shadow[12]>64'sd17592186044416||shadow[12]< -64'sd17592186044416||
      shadow[9]>ONE||shadow[9]< -ONE||shadow[10]>ONE||shadow[10]< -ONE||
      shadow[11]>ONE||shadow[11]< -ONE||(shadow[9]==0&&shadow[10]==0&&shadow[11]==0))cfg_error<=1;
    else begin for(i=0;i<36;i=i+1)c[i]<=shadow[i];epoch<=cfg_epoch;cfg_ready<=1;end
   end
   if(state!=IDLE&&state!=OUTPUT)point_cycles<=point_cycles+1;
   if(out_valid&&out_ready)begin out_valid<=0;state<=IDLE;end
   case(state)
    IDLE:if(in_valid&&in_ready)begin
     id<=candidate_id;modes<=mode_bits;reason<=0;out_epoch<=epoch;point_cycles<=0;
     for(i=0;i<3;i=i+1)begin cam[i]<=0;axis[i]<=0;end
     if(!cfg_ready)begin reason<=6;state<=EMIT;end
     else if(c[34][3])begin reason<=7;state<=EMIT;end
     else if(u_q12>=32'd3080192||v_q12>=32'd1966080)begin reason<=8;state<=EMIT;end
     else begin xd<=($signed({1'b0,u_q12})<<<18)-c[0];yd<=($signed({1'b0,v_q12})<<<18)-c[1];state<=NX;end
    end
    WAIT_MUL:if(mul_done)begin
     m<=mul_value;full_product<=mul_raw;
     if(mul_overflow)begin reason<=5;state<=EMIT;end else state<=resume;
    end
    WAIT_DIV:begin
     ydy<=yd-dy;
     if(div_done)begin
      m<=div_value;
      if(div_overflow||div_zero)begin reason<=5;state<=EMIT;end else state<=resume;
     end
    end
    NX:multiply(xd,c[2],NX_DONE);
    NX_DONE:begin xd<=m;x<=m;state<=NY;end
    NY:multiply(yd,c[3],NY_DONE);
    NY_DONE:begin yd<=m;y<=m;iteration<=0;state<=B_X2;end
    B_X2:multiply(x,x,B_Y2);
    B_Y2:begin x2<=m;multiply(y,y,B_XY);end
    B_XY:begin y2<=m;r2<=x2+m;multiply(x,y,B_R2);end
    B_R2:begin xy<=m;r2_bad<=r2<0||r2>c[35];state<=B_R4;end
    B_R4:begin
     r2x2<=r2+(x2<<<1);r2y2<=r2+(y2<<<1);
     if(r2_bad)begin reason<=9;state<=EMIT;end
     else if(!c[34][2]||iteration==5)state<=Q_X;else multiply(r2,r2,B_R6);
    end
    B_R6:begin r4<=m;multiply(m,r2,B_K1);end
    B_K1:begin r6<=m;multiply(c[4],r2,B_K2);end
    B_K2:begin radial<=ONE+m;multiply(c[5],r4,B_K3);end
    B_K3:begin radial<=radial+m;multiply(c[8],r6,B_DX1);end
    B_DX1:begin radial<=radial+m;multiply(c[6],xy,B_DX2);end
    B_DX2:begin dx<=m<<<1;multiply(c[7],r2x2,B_DY1);end
    B_DY1:begin dx<=dx+m;multiply(c[6],r2y2,B_DY2);end
    B_DY2:begin dy<=m;xdx<=xd-dx;multiply(c[7],xy,B_IX);end
    B_IX:begin dy<=dy+(m<<<1);if(radial<=0)begin reason<=9;state<=EMIT;end else divide(xdx,radial,B_IY);end
    B_IY:begin x<=m;divide(ydy,radial,B_NEXT);end
    B_NEXT:begin y<=m;iteration<=iteration+1;state<=B_X2;end
    Q_X:multiply(c[9],x,Q_Y);
    Q_Y:begin q<=m;multiply(c[10],y,Q_SUM);end
    Q_SUM:begin q<=q+m+c[11];state<=Q_SQUARE;end
    Q_SQUARE:multiply(q,q,L_X);
    L_X:begin q_squared<=full_product;multiply(x,x,L_Y);end
    L_Y:begin l2<=m;multiply(y,y,L_SUM);end
    L_SUM:begin l2<=l2+m+ONE;state<=PARALLEL;end
    PARALLEL:begin nc12<=-c[12];multiply(c[13],l2,PAR_CHECK);end
    PAR_CHECK:if((q_squared<<<30)<=full_product)begin reason<=1;state<=EMIT;end else divide(nc12,q,DEPTH);
    DEPTH:if(m<=0)begin reason<=2;state<=EMIT;end else begin t<=m;cam[2]<=m;state<=CAM_X;end
    CAM_X:multiply(t,x,CAM_Y);
    CAM_Y:begin cam[0]<=m;multiply(t,y,CAM_DONE);end
    CAM_DONE:begin cam[1]<=m;state<=R_X;end
    R_X:if(c[34][0])multiply(cam[0],cam[0],R_Y);else state<=BOUNDS;
    R_Y:begin temp<=m;multiply(cam[1],cam[1],R_Z);end
    R_Z:begin temp<=temp+m;multiply(cam[2],cam[2],R_CHECK);end
    // Preserve signed64 wrap before comparison; register sum separately from range/next-state logic.
    R_CHECK:begin temp<=temp+m;state<=R_RANGE;end
    R_RANGE:if(temp<c[14]||temp>c[15])begin reason<=3;state<=EMIT;end else state<=BOUNDS;
    BOUNDS:if(c[34][1]&&((cam[0]<c[16])||(cam[1]<c[17])||(cam[2]<c[18])||
       (cam[0]>c[19])||(cam[1]>c[20])||(cam[2]>c[21])))begin reason<=4;state<=EMIT;end
     else begin row<=0;col<=0;accumulator<=c[31];state<=ROTATE;end
    ROTATE:multiply(cam[col],c[22+col*3+row],ROTATE_ADD);
    ROTATE_ADD:begin accumulator<=accumulator+m;state<=ROTATE_NEXT;end
    ROTATE_NEXT:if(col==2)begin
     axis[row]<=accumulator;
     if(row==2)state<=EMIT;else begin row<=row+1;col<=0;accumulator<=c[32+row];state<=ROTATE;end
    end else begin col<=col+1;state<=ROTATE;end
    EMIT:begin
     // Preserve EMIT as the common entry for success and every error branch.
     for(i=0;i<3;i=i+1)begin
      camera_rounded[i]<=q16_round51(cam[i]);axis_rounded[i]<=q16_round51(axis[i]);
     end
     state<=EMIT_CHECK;
    end
    EMIT_CHECK:begin
     for(i=0;i<3;i=i+1)begin camera_q16[i]<=camera_rounded[i][31:0];axis_q16[i]<=axis_rounded[i][31:0];end
     camera_overflow<=(camera_rounded[0][50:32]!={19{camera_rounded[0][31]}})||
      (camera_rounded[1][50:32]!={19{camera_rounded[1][31]}})||
      (camera_rounded[2][50:32]!={19{camera_rounded[2][31]}});
     axis_overflow<=(axis_rounded[0][50:32]!={19{axis_rounded[0][31]}})||
      (axis_rounded[1][50:32]!={19{axis_rounded[1][31]}})||
      (axis_rounded[2][50:32]!={19{axis_rounded[2][31]}});
     state<=EMIT_PACK;
    end
    EMIT_PACK:begin
     out_record<=0;out_record[31:0]<=id;
     if(reason!=0||camera_overflow||axis_overflow)begin out_record[255:224]<={8'b0,(reason!=0?reason:8'd5),14'b0,modes};end
     else begin
      out_record[63:32]<=camera_q16[0];out_record[95:64]<=camera_q16[1];out_record[127:96]<=camera_q16[2];
      out_record[159:128]<=axis_q16[0];out_record[191:160]<=axis_q16[1];out_record[223:192]<=axis_q16[2];
      out_record[255:224]<=32'h100|{30'b0,modes};
     end
     out_valid<=1;state<=OUTPUT;
    end
    OUTPUT:begin end
    default:begin reason<=10;state<=EMIT;end
   endcase
  end
 end
endmodule
`default_nettype wire
