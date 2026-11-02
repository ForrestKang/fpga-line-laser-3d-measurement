`timescale 1ns/1ps
// Row-local arithmetic. Row buffering and a sliding eight-sample background
// preserve all Q12 residual bits. Radix-4 division occurs only at run boundaries.
module pfo3_row_r25 #(parameter W=752, H=480)(
 input wire clk,rst,clear,
 input wire [7:0] pixel_data,
 input wire pixel_valid,
 output wire pixel_ready,
 output reg [511:0] diag_data,
 output reg diag_valid,
 input wire diag_ready,
 output reg slot_we,
 output reg [12:0] slot_addr,
 output reg [95:0] slot_data,
 output reg row_we,
 output reg [8:0] row_addr,
 output reg [191:0] row_data,
 output reg finished,
 output reg [31:0] raw_count,qualified_count,sat250_count,sat255_count,
 output reg overflow,
 output reg border_seen
);
 localparam LOAD=0, BG=1, SCAN=2, CLOSE=3, DIVIDE=4, EMIT=5, ROW=6, FIN=7,
  FETCH=8,RESIDUAL=9,DETECT=10,ADDRESS=11;
 reg [3:0] state;
 (* ram_style="distributed" *) reg [7:0] pixels[0:W-1];
 reg [9:0] load_x;
 reg [8:0] y;
 reg signed [11:0] scan_x;
 reg [3:0] bg_i;
 reg [11:0] left_sum,right_sum;
 reg [20:0] r0,r1,r2,r3;
 reg [11:0] l0,l1,rr0,rr1;
 reg active;
 reg [9:0] first,last;
 reg [31:0] sum0;
 reg [47:0] sum1;
 reg [20:0] peak, first_d,last_d,peak_asym,max_r,outside_left,outside_right,prior_detector;
 reg [7:0] peak_raw;
 reg [9:0] sat_run;
 reg [9:0] row_raw,row_local,row_sat250,row_sat255;
 reg [7:0] row_peak;
 // Plateau maximum detector: previous plateau is finalized on a descent.
 reg [20:0] prev_d, retained_peak, valley;
 reg [9:0] plateau_x,retained_x;
 reg [7:0] peak_count;
 reg rising, have_peak;
 reg [6:0] reason;
 // M*4096 < 2^52 for 752 uint8 pixels. Three prealigned divisors avoid
 // a variable barrel shifter, digit multiplier and second carry chain.
 reg [51:0] remainder,divisor1,divisor2,divisor3;
 reg [21:0] quotient;
 reg [3:0] div_step;
 reg [31:0] saved_id;
 integer ix,cx;
 reg [20:0] residual,detector;
 reg [31:0] smooth_sum;
 reg [12:0] background;
 reg [20:0] asym;
 reg [7:0] raw;
 reg [7:0] next_peak_count;
 reg [7:0] fetched_pixel,fetched_center,left_out,left_in,right_out,right_in;
 reg [9:0] pixel_address,center_address,left_out_address,left_in_address,right_out_address,right_in_address;
 reg [20:0] residual_reg,detector_reg,asym_reg;
 reg [30:0] moment_reg;
 reg [20:0] next_retained_peak,next_valley;
 reg [9:0] next_retained_x;
 reg next_have;
 reg [52:0] diff1,diff2,diff3;
 reg [1:0] digit;
 reg [7:0] terminal_peak_count;
 function automatic integer clamp(input integer a);
 begin if(a<0)clamp=0;else if(a>=W)clamp=W-1;else clamp=a;end
 endfunction
 always @* begin
  ix=clamp(scan_x); cx=scan_x-2;
  raw=fetched_center;
  background=left_sum+right_sum;
  if(({24'b0,fetched_pixel}<<12)>(background<<9))
   residual=(fetched_pixel<<12)-(background<<9); else residual=0;
  smooth_sum=residual_reg+(r0<<2)+(r1<<2)+(r1<<1)+(r2<<2)+r3;
  detector=detector_reg;asym=asym_reg;
  next_peak_count=peak_count;next_retained_peak=retained_peak;
  next_retained_x=retained_x;next_have=have_peak;
  next_valley=valley;
  if(prev_d<next_valley)next_valley=prev_d;
  if(active && detector<prev_d && rising && prev_d>=49152)begin
   if(!have_peak)begin next_peak_count=1;next_have=1;
    next_retained_peak=prev_d;next_retained_x=plateau_x;next_valley=prev_d;end
   else if(plateau_x>=retained_x+3 &&
     next_valley+16384<=((retained_peak<prev_d)?retained_peak:prev_d))begin
    next_peak_count=peak_count+1;next_retained_peak=prev_d;
    next_retained_x=plateau_x;next_valley=prev_d;
   end else if(prev_d>retained_peak)begin next_retained_peak=prev_d;
    next_retained_x=plateau_x;next_valley=prev_d;end
  end
  diff1={1'b0,remainder}-{1'b0,divisor1};
  diff2={1'b0,remainder}-{1'b0,divisor2};
  diff3={1'b0,remainder}-{1'b0,divisor3};
  if(!diff3[52])digit=3;
  else if(!diff2[52])digit=2;
  else if(!diff1[52])digit=1;else digit=0;
  terminal_peak_count=peak_count;
  if(last==W-1&&rising&&prev_d>=49152)begin
   if(!have_peak)terminal_peak_count=1;
   else if(plateau_x>=retained_x+3&&valley+16384<=((retained_peak<prev_d)?retained_peak:prev_d))
    terminal_peak_count=peak_count+1;
  end
 end
 assign pixel_ready=state==LOAD && !finished;
 always @(posedge clk)begin
  slot_we<=0;row_we<=0;
  if(rst||clear)begin
   state<=LOAD;load_x<=0;y<=0;scan_x<=0;bg_i<=0;left_sum<=0;right_sum<=0;
   r0<=0;r1<=0;r2<=0;r3<=0;l0<=0;l1<=0;rr0<=0;rr1<=0;
   active<=0;diag_valid<=0;finished<=0;raw_count<=0;qualified_count<=0;
   sat250_count<=0;sat255_count<=0;overflow<=0;border_seen<=0;
   row_raw<=0;row_local<=0;row_sat250<=0;row_sat255<=0;row_peak<=0;
   peak_count<=0;have_peak<=0;rising<=0;prev_d<=0;prior_detector<=0;
   div_step<=0;divisor1<=0;divisor2<=0;divisor3<=0;remainder<=0;quotient<=0;
   fetched_pixel<=0;fetched_center<=0;left_out<=0;left_in<=0;right_out<=0;right_in<=0;
   pixel_address<=0;center_address<=0;left_out_address<=0;left_in_address<=0;right_out_address<=0;right_in_address<=0;
   residual_reg<=0;detector_reg<=0;asym_reg<=0;moment_reg<=0;
  end else case(state)
   LOAD: if(pixel_valid&&pixel_ready)begin
    pixels[load_x]<=pixel_data;
    if(pixel_data>row_peak)row_peak<=pixel_data;
    if(pixel_data>=250)begin row_sat250<=row_sat250+1;sat250_count<=sat250_count+1;end
    if(pixel_data==255)begin row_sat255<=row_sat255+1;sat255_count<=sat255_count+1;end
    if(load_x==W-1)begin load_x<=0;state<=BG;bg_i<=0;left_sum<=0;right_sum<=0;end
    else load_x<=load_x+1;
   end
   BG: begin
    left_sum<=left_sum+pixels[0];right_sum<=right_sum+pixels[clamp(8+bg_i)];
    if(bg_i==3)begin state<=ADDRESS;scan_x<=-2;r0<=0;r1<=0;r2<=0;r3<=0;active<=0;prior_detector<=0;end
    else bg_i<=bg_i+1;
   end
   ADDRESS:begin
    pixel_address<=clamp(scan_x);center_address<=clamp(scan_x-2);
    left_out_address<=clamp(scan_x-11);left_in_address<=clamp(scan_x-7);
    right_out_address<=clamp(scan_x+8);right_in_address<=clamp(scan_x+12);
    state<=FETCH;
   end
   FETCH:begin
    fetched_pixel<=pixels[pixel_address];fetched_center<=pixels[center_address];
    left_out<=pixels[left_out_address];left_in<=pixels[left_in_address];
    right_out<=pixels[right_out_address];right_in<=pixels[right_in_address];
    state<=RESIDUAL;
   end
   RESIDUAL:begin residual_reg<=residual;state<=DETECT;end
   DETECT:begin
    detector_reg<=smooth_sum>>4;
    if(l1>=rr1)asym_reg<=(l1-rr1)<<9;else asym_reg<=(rr1-l1)<<9;
    moment_reg<=cx[9:0]*r1;
    state<=SCAN;
   end
   SCAN: begin
    state<=ADDRESS;
    // Negative and tail samples replicate residual endpoints, not detector output.
    r3<=r2;r2<=r1;r1<=r0;r0<=residual_reg;
    l1<=l0;l0<=left_sum;rr1<=rr0;rr0<=right_sum;
    if(scan_x>=0 && scan_x<W-1)begin
     left_sum<=left_sum-left_out+left_in;
     right_sum<=right_sum-right_out+right_in;
    end
    scan_x<=scan_x+1;
    if(cx>=0 && cx<W)begin
     prior_detector<=detector;
     if(detector>=24576)begin
      if(!active)begin
       active<=1;first<=cx;last<=cx;sum0<=r1;sum1<=moment_reg;
       peak<=detector;first_d<=detector;last_d<=detector;peak_asym<=asym;max_r<=r1;
       outside_left<=(cx==0)?detector:prior_detector;
       peak_raw<=raw;sat_run<=raw>=250;prev_d<=detector;plateau_x<=cx;
       rising<=1;peak_count<=0;have_peak<=0;valley<=detector;
       retained_peak<=0;retained_x<=0;
      end else begin
       last<=cx;sum0<=sum0+r1;sum1<=sum1+moment_reg;last_d<=detector;
       if(detector>peak)begin peak<=detector;peak_asym<=asym;end
       if(r1>max_r)max_r<=r1;
       if(raw>peak_raw)peak_raw<=raw;
       if(raw>=250)sat_run<=sat_run+1;
       if(detector!=prev_d)begin
        rising<=detector>prev_d;plateau_x<=cx;
       end
       peak_count<=next_peak_count;have_peak<=next_have;
       retained_peak<=next_retained_peak;retained_x<=next_retained_x;valley<=next_valley;
       prev_d<=detector;
      end
     end else if(active)begin
      peak_count<=next_peak_count;active<=0;state<=CLOSE;outside_right<=detector;
     end
    end
    if(cx==W-1)begin
     if(detector>=24576)begin state<=CLOSE;outside_right<=peak;end
     else if(!active)state<=ROW;
    end
   end
   CLOSE: begin
    if(first<13||last>W-14)border_seen<=1;
    reason<=((peak<49152)?1:0)|((last-first+1<3||last-first+1>17)?2:0)|
      ((first<13||last>W-14)?4:0)|((sat_run!=0)?8:0)|
      ((terminal_peak_count>1)?16:0)|((sum0==0)?32:0)|((peak_asym>49152)?64:0);
    peak_count<=terminal_peak_count;
    remainder<={sum1[39:0],12'b0};
    divisor1<={sum0,20'b0};divisor2<=({20'b0,sum0}<<21);
    divisor3<=({20'b0,sum0}+({20'b0,sum0}<<1))<<20;
    quotient<=0;div_step<=0;
    saved_id<=raw_count;raw_count<=raw_count+1;row_raw<=row_raw+1;
    if(sum0==0)state<=EMIT;else state<=DIVIDE;
   end
   DIVIDE: begin
    case(digit)
     3:remainder<=diff3[51:0];2:remainder<=diff2[51:0];1:remainder<=diff1[51:0];
     default:remainder<=remainder;
    endcase
    divisor1<=divisor1>>2;divisor2<=divisor2>>2;divisor3<=divisor3>>2;
    quotient<=(quotient<<2)|digit;
    if(div_step==10)state<=EMIT;else div_step<=div_step+1;
   end
   EMIT: begin
    if(!diag_valid)begin
     diag_data<=0;
     diag_data[31:0]<=saved_id;diag_data[63:32]<=y;diag_data[95:64]<=first;
     diag_data[127:96]<=last;diag_data[159:128]<=quotient;
     diag_data[191:160]<=sum0;diag_data[223:192]<=sum1[31:0];
     diag_data[255:224]<=sum1[47:32];diag_data[287:256]<=peak_raw;
     diag_data[319:288]<=sat_run;diag_data[351:320]<=max_r;
     diag_data[383:352]<=(first==0||last==W-1)?0:peak-((outside_left>outside_right)?outside_left:outside_right);
     diag_data[415:384]<=peak;
     diag_data[447:416]<={(sum0==0?8'b0:8'b1),((reason==0&&row_local>=16)?8'd6:8'd1),((reason==0&&row_local>=16)?8'd6:8'd1),1'b0,reason};
     diag_data[479:448]<=32'hffffffff;diag_data[511:480]<={peak_count,24'b0};
     diag_valid<=1;
     if(reason==0)begin
      qualified_count<=qualified_count+1;row_local<=row_local+1;
      if(row_local<16)begin
       slot_we<=1;slot_addr<=y*16+row_local;
       slot_data<={18'b0,peak_raw,peak[19:0],10'(last-first+1),quotient,saved_id[17:0]};
      end else overflow<=1;
     end
    end else if(diag_ready)begin
     diag_valid<=0;
     if(scan_x>=W+2)state<=ROW;else state<=ADDRESS;
    end
   end
   ROW: begin
    row_we<=1;row_addr<=y;
    row_data<=0;row_data[4:0]<=row_local>16?16:row_local;
    row_data[8]<=row_local>16;row_data[63:32]<=row_peak;
    row_data[95:64]<=row_sat250;row_data[127:96]<=row_sat255;
    row_data[159:128]<=row_raw;row_data[191:160]<=row_local;
    if(y==H-1)begin state<=FIN;finished<=1;end
    else begin y<=y+1;state<=LOAD;row_raw<=0;row_local<=0;row_sat250<=0;row_sat255<=0;row_peak<=0;end
   end
   FIN: finished<=1;
   default:state<=FIN;
  endcase
 end
endmodule
