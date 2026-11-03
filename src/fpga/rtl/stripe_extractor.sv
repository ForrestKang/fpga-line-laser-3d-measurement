`timescale 1ns/1ps
// Shared complete-run detector, generated independent direction engines and
// bounded, ordered candidate retirement. Row barriers preserve packet summaries.
module stripe_extractor #(parameter W=752,H=480,PREFETCH=1,LANES=2,QUEUE_DEPTH=4,DISPATCH=0,BANKED_REQUESTS=0,BANKED_RESULTS=0,BALANCED_DISPATCH=0,SEED_RETRY=1)(
 input wire clk,rst,clear,input wire bin,input wire seed_retry_enable,input wire [7:0] pixel_data,input wire pixel_valid,
 output wire pixel_ready,output reg [511:0] diag_data,output reg diag_valid,input wire diag_ready,
 output reg slot_we,output reg [12:0] slot_addr,output reg [95:0] slot_data,
 output reg row_we,output reg [8:0] row_addr,output reg [191:0] row_data,
 output reg finished,output reg [31:0] raw_count,qualified_count,sat250_count,sat255_count,
 output reg overflow,output reg border_seen
);
 localparam DEPTH=LANES*QUEUE_DEPTH, AW=$clog2(DEPTH), QW=$clog2(QUEUE_DEPTH);
 localparam SCAN=0,CLOSE=1,DRAIN=2,ROW=3,FIN=4;
 // live measurement binning mode: WLR binned pixels per row (bin=0: W, the reference path).
 wire [9:0] WLR=bin?10'(W/2):10'(W);
 reg [2:0] state;
 // One-entry elastic feature stage. Strength and threshold are evaluated
 // before the register, independently of the stateful run/peak logic.
 wire wf_valid,wf_ready,wfinished,wf_border;
 wire [9:0] wf_x; wire [8:0] wf_y; wire [7:0] wf_raw;
 wire [11:0] wf_smooth; wire signed [11:0] wf_gx,wf_gy,wf_xx,wf_xy,wf_yy;
 wire [3:0] wf_sats;
 reg fv,fb,hit;
 wire fr=state==SCAN&&!finished;
 reg [9:0] fx; reg [8:0] fy; reg [7:0] raw;
 reg signed [11:0] gx,gy,xx,xy,yy;
 reg [3:0] sats; reg [11:0] strength,smooth;
 wire signed [12:0] neg_xx=-$signed({wf_xx[11],wf_xx});
 wire signed [12:0] neg_yy=-$signed({wf_yy[11],wf_yy});
 wire [11:0] wf_strength=wf_border?12'd0:((neg_xx>neg_yy)?(neg_xx>0?neg_xx[11:0]:12'd0):(neg_yy>0?neg_yy[11:0]:12'd0));
 assign wf_ready=!fv||fr;
 shared_window_filter #(.W(W),.H(H)) window_core(
  .clk(clk),.rst(rst),.clear(clear),.half(bin),.pixel_data(pixel_data),.pixel_valid(pixel_valid),.pixel_ready(pixel_ready),
  .feat_valid(wf_valid),.feat_ready(wf_ready),.feat_x(wf_x),.feat_y(wf_y),.feat_raw(wf_raw),.feat_smooth_q4(wf_smooth),
  .feat_gx_q2(wf_gx),.feat_gy_q2(wf_gy),.feat_hxx_q2(wf_xx),.feat_hxy_q2(wf_xy),.feat_hyy_q2(wf_yy),
  .feat_sat_count(wf_sats),.feat_border(wf_border),.finished(wfinished));
 always @(posedge clk)begin
  if(rst||clear)begin
   fv<=0;fb<=0;hit<=0;fx<=0;fy<=0;raw<=0;
   gx<=0;gy<=0;xx<=0;xy<=0;yy<=0;sats<=0;strength<=0;smooth<=0;
  end else if(wf_ready)begin
   fv<=wf_valid;
   if(wf_valid)begin
    fb<=wf_border;fx<=wf_x;fy<=wf_y;raw<=wf_raw;sats<=wf_sats;
    gx<=wf_gx;gy<=wf_gy;xx<=wf_xx;xy<=wf_xy;yy<=wf_yy;
    smooth<=wf_smooth;strength<=wf_strength;hit<=!wf_border&&wf_strength>=24;
   end
  end
 end
 reg active,row_pending;
 reg [9:0] first,last,best_x;
 reg [8:0] current_y,best_y;
 reg [11:0] best_strength;
 reg [9:0] retry_x;
 reg [11:0] retry_smooth,retry_strength;
 reg signed [11:0] retry_gx,retry_gy,retry_xx,retry_xy,retry_yy;
 reg [81:0] retry_metadata[0:DEPTH-1];
 reg [7:0] peak_raw,row_peak;
 reg [9:0] sat_run;
 reg [31:0] sum_strength;
 reg [9:0] row_raw,row_local,row_sat250,row_sat255;
 reg signed [11:0] best_gx,best_gy,best_xx,best_xy,best_yy;
 reg [11:0] prev_strength,retained_peak,valley;
 reg [9:0] plateau_x,retained_x;
 reg [7:0] peak_count;
 reg rising,have_peak;
 wire [6:0] local_reason=((best_strength<48)?1:0)|((last-first+1>17)?2:0)|
  ((first<2||last>WLR-3||best_y<1||best_y>H-2)?4:0)|((sat_run!=0)?8:0)|((peak_count>1)?16:0);

 // Metadata never changes between enqueue and retirement. Results are written
 // by exactly one lane. A slot is reusable only after its diagnostic handshake.
 reg [207:0] metadata[0:DEPTH-1];
 reg [51:0] results[0:DEPTH-1];
 reg [DEPTH-1:0] completed;
 reg [AW-1:0] tail,head;
 reg [AW:0] occupancy;
 reg [AW-1:0] requests[0:LANES-1][0:QUEUE_DEPTH-1];
 // Optional lane-local Hessian/gradient FIFO removes N asynchronous read ports
 // from the global metadata ring. The ring remains the sole retirement source.
 reg [QW-1:0] qhead[0:LANES-1],qtail[0:LANES-1];
 reg [QW:0] qcount[0:LANES-1];
 reg [AW-1:0] running_id[0:LANES-1];
 wire [51:0] bank_result_read[0:LANES-1];
 wire [LANES-1:0] lane_ready,lane_start,lane_done,lane_center_valid,lane_retry_used;
 wire signed [12:0] lane_du[0:LANES-1],lane_dv[0:LANES-1];
 wire signed [11:0] lane_nx[0:LANES-1],lane_ny[0:LANES-1];
 // Constant column partitions do not infer a variable divider. A complete run
 // is assigned by its strongest pixel, so a run crossing a partition remains
 // one candidate and keeps its original global identifier.
 localparam LW=(LANES>1)?$clog2(LANES):1;
 reg [LW-1:0] selected_lane,dispatch_next;
 reg [LW-1:0] result_owner[0:DEPTH-1];
 localparam LOAD_W=$clog2(QUEUE_DEPTH+2),KEY_W=LOAD_W+LW;
 wire [KEY_W-1:0] dispatch_key[0:2*LANES-2];
 wire [LW-1:0] dispatch_id[0:2*LANES-2];
 genvar dispatch_g;
 generate if(BALANCED_DISPATCH)begin: balanced_dispatch
  for(dispatch_g=0;dispatch_g<LANES;dispatch_g=dispatch_g+1)begin: leaves
   wire [LOAD_W-1:0] load=qcount[dispatch_g]+LOAD_W'(!lane_ready[dispatch_g]);
   wire [LW-1:0] distance=LW'(dispatch_g)-dispatch_next;
   assign dispatch_key[LANES-1+dispatch_g]={load,distance};
   assign dispatch_id[LANES-1+dispatch_g]=LW'(dispatch_g);
  end
  for(dispatch_g=0;dispatch_g<LANES-1;dispatch_g=dispatch_g+1)begin: tree
   wire take_left=dispatch_key[2*dispatch_g+1]<dispatch_key[2*dispatch_g+2];
   assign dispatch_key[dispatch_g]=take_left?dispatch_key[2*dispatch_g+1]:dispatch_key[2*dispatch_g+2];
   assign dispatch_id[dispatch_g]=take_left?dispatch_id[2*dispatch_g+1]:dispatch_id[2*dispatch_g+2];
  end
 end else begin: unbalanced_dispatch
  for(dispatch_g=0;dispatch_g<2*LANES-1;dispatch_g=dispatch_g+1)begin: unused_tree
   assign dispatch_key[dispatch_g]=0;assign dispatch_id[dispatch_g]=0;
  end
 end endgenerate
 integer part,dispatch_index,lane_load,min_load;
 always @*begin
  selected_lane=0;dispatch_index=0;lane_load=0;min_load=QUEUE_DEPTH+2;
  if(DISPATCH==0)begin
   for(part=1;part<LANES;part=part+1)
    if(best_x>=(bin?(part*(W/2)+LANES-1)/LANES:(part*W+LANES-1)/LANES))selected_lane=part;
  end else if(BALANCED_DISPATCH)begin
   selected_lane=dispatch_id[0];
  end else begin
   // parallel optional work-conserving dispatch. A queued or executing request
   // counts once. Rotating ties avoid pinning clustered stripes to lane zero.
   // Ordered metadata retirement below is unchanged, so IDs/records stay stable.
   for(part=0;part<LANES;part=part+1)begin
    dispatch_index=(dispatch_next+part)%LANES;
    lane_load=qcount[dispatch_index]+(lane_ready[dispatch_index]?0:1);
    if(lane_load<min_load)begin min_load=lane_load;selected_lane=dispatch_index;end
   end
  end
 end
 wire enqueue=state==CLOSE && occupancy<DEPTH && qcount[selected_lane]<QUEUE_DEPTH;
 wire dequeue=diag_valid&&diag_ready;
 genvar g;
 generate for(g=0;g<LANES;g=g+1)begin: direction_lanes
  wire [AW-1:0] rid=requests[g][qhead[g]];
  wire [207:0] req=metadata[rid];
  wire [59:0] features;
  if(BANKED_REQUESTS)begin: local_requests
   (* ram_style="distributed" *) reg [59:0] feature_fifo[0:QUEUE_DEPTH-1];
   // Head/tail/count clear with the frame; payload RAM needs no reset.
   always @(posedge clk)
    if(enqueue&&selected_lane==g)feature_fifo[qtail[g]]<={best_yy,best_xy,best_xx,best_gy,best_gx};
   assign features=feature_fifo[qhead[g]];
  end else begin: shared_requests
   assign features=req[207:148];
  end
  if(BANKED_RESULTS)begin: local_results
   (* ram_style="distributed" *) reg [51:0] result_fifo[0:DEPTH-1];
   always @(posedge clk)
    if(!rst&&!clear&&lane_done[g])result_fifo[running_id[g]]<={lane_retry_used[g],lane_center_valid[g],lane_ny[g],lane_nx[g],lane_dv[g],lane_du[g]};
   assign bank_result_read[g]=result_fifo[head];
  end else begin: shared_results
   assign bank_result_read[g]=52'd0;
  end
  assign lane_start[g]=qcount[g]!=0&&lane_ready[g];
  if(SEED_RETRY)begin: bounded_retry
   wire [81:0] alt_req=retry_metadata[rid];
   direction_retry core(.clk(clk),.rst(rst),.clear(clear),.start(lane_start[g]),
    .features(features),.alternate(alt_req[59:0]),
    .eligible(seed_retry_enable && req[147:141]==0 && alt_req[81:70]>=48 && alt_req[69:60]!=req[61:52]),
    .ready(lane_ready[g]),.done(lane_done[g]),.center_valid(lane_center_valid[g]),
    .retry_used(lane_retry_used[g]),.du_q12(lane_du[g]),.dv_q12(lane_dv[g]),
    .normal_x(lane_nx[g]),.normal_y(lane_ny[g]));
  end else begin: frozen_single_seed
   assign lane_retry_used[g]=0;
   direction_refiner core(.clk(clk),.rst(rst),.clear(clear),.start(lane_start[g]),
    .gx($signed(features[11:0])),.gy($signed(features[23:12])),
    .hxx($signed(features[35:24])),.hxy($signed(features[47:36])),.hyy($signed(features[59:48])),
    .ready(lane_ready[g]),.done(lane_done[g]),.center_valid(lane_center_valid[g]),
    .du_q12(lane_du[g]),.dv_q12(lane_dv[g]),.normal_x(lane_nx[g]),.normal_y(lane_ny[g]));
  end
 end endgenerate
 wire [207:0] m=metadata[head];
 wire [51:0] r=BANKED_RESULTS?bank_result_read[result_owner[head]]:results[head];
 wire signed [12:0] du=$signed(r[12:0]),dv=$signed(r[25:13]);
 wire signed [11:0] nx=$signed(r[37:26]),ny=$signed(r[49:38]);
 wire cv=r[50];
 wire [81:0] alt_m=retry_metadata[head];
 wire [9:0] center_x=r[51]?alt_m[69:60]:m[61:52];
 wire [23:0] center_gradient=r[51]?alt_m[23:0]:m[171:148];
 wire [21:0] uq=$signed({2'b0,center_x,12'b0})+$signed(du);
 // live measurement: wire coordinates of a binned candidate in W pixels (pair 2j,2j+1 -> centre 2u+0.5).
 wire [22:0] uq_full={uq,1'b0}+23'd2048;
 wire [23:0] vq=$signed({3'b0,m[70:62],12'b0})+$signed(dv);
 wire [19:0] peak_q12=m[82:71]>1023?20'hfffff:{m[80:71],10'b0};
 wire [6:0] final_reason=m[147:141]|(!cv?7'd32:7'd0);
 reg [7:0] next_peak_count;
 reg [11:0] next_retained_peak,next_valley;
 reg [9:0] next_retained_x;
 reg next_have;
 integer k;
 always @*begin
  next_peak_count=peak_count;next_retained_peak=retained_peak;next_retained_x=retained_x;next_have=have_peak;next_valley=valley;
  if(prev_strength<next_valley)next_valley=prev_strength;
  if(active&&strength<prev_strength&&rising&&prev_strength>=48)begin
   if(!have_peak)begin next_peak_count=1;next_have=1;next_retained_peak=prev_strength;next_retained_x=plateau_x;next_valley=prev_strength;end
   else if(plateau_x>=retained_x+3&&next_valley+16<=((retained_peak<prev_strength)?retained_peak:prev_strength))begin
    next_peak_count=peak_count+1;next_retained_peak=prev_strength;next_retained_x=plateau_x;next_valley=prev_strength;
   end else if(prev_strength>retained_peak)begin next_retained_peak=prev_strength;next_retained_x=plateau_x;next_valley=prev_strength;end
  end
 end
 always @(posedge clk)begin
  slot_we<=0;row_we<=0;
  if(rst||clear)begin
   state<=SCAN;active<=0;row_pending<=0;current_y<=0;best_y<=0;first<=0;last<=0;best_x<=0;best_strength<=0;
   peak_raw<=0;row_peak<=0;sat_run<=0;sum_strength<=0;
   row_raw<=0;row_local<=0;row_sat250<=0;row_sat255<=0;
   best_gx<=0;best_gy<=0;best_xx<=0;best_xy<=0;best_yy<=0;
   retry_x<=0;retry_smooth<=0;retry_strength<=0;
   retry_gx<=0;retry_gy<=0;retry_xx<=0;retry_xy<=0;retry_yy<=0;
   prev_strength<=0;retained_peak<=0;valley<=0;plateau_x<=0;retained_x<=0;peak_count<=0;rising<=0;have_peak<=0;
   diag_data<=0;diag_valid<=0;slot_addr<=0;slot_data<=0;row_addr<=0;row_data<=0;finished<=0;
   raw_count<=0;qualified_count<=0;sat250_count<=0;sat255_count<=0;overflow<=0;border_seen<=0;
   head<=0;tail<=0;occupancy<=0;completed<=0;dispatch_next<=0;
   for(k=0;k<LANES;k=k+1)begin qhead[k]<=0;qtail[k]<=0;qcount[k]<=0;running_id[k]<=0;end
  end else begin
   if(enqueue)begin dispatch_next<=selected_lane+1'b1;if(BANKED_RESULTS)result_owner[tail]<=selected_lane;end
   case({enqueue,dequeue})
    2'b10:occupancy<=occupancy+1;
    2'b01:occupancy<=occupancy-1;
    default:occupancy<=occupancy;
   endcase
   for(k=0;k<LANES;k=k+1)begin
    case({enqueue&&selected_lane==k,lane_start[k]})
     2'b10:qcount[k]<=qcount[k]+1;
     2'b01:qcount[k]<=qcount[k]-1;
     default:qcount[k]<=qcount[k];
    endcase
    if(enqueue&&selected_lane==k)begin
     requests[k][qtail[k]]<=tail;qtail[k]<=qtail[k]+1;
    end
    if(lane_start[k])begin running_id[k]<=requests[k][qhead[k]];qhead[k]<=qhead[k]+1;end
    if(lane_done[k])begin
     if(!BANKED_RESULTS)results[running_id[k]]<={lane_retry_used[k],lane_center_valid[k],lane_ny[k],lane_nx[k],lane_dv[k],lane_du[k]};
     completed[running_id[k]]<=1;
    end
   end
   if(dequeue)begin diag_valid<=0;head<=head+1;end
   if(!diag_valid && occupancy!=0 && completed[head])begin
    diag_data<=0;diag_data[31:0]<=m[31:0];diag_data[63:32]<=m[70:62];
    diag_data[95:64]<=bin?{m[41:32],1'b0}:m[41:32];diag_data[127:96]<=bin?{m[51:42],1'b1}:m[51:42];
    diag_data[159:128]<=bin?uq_full:uq;diag_data[191:160]<=vq;
    diag_data[223:192]<={8'b0,center_gradient};diag_data[255:224]<={8'b0,ny,nx};
    diag_data[287:256]<=m[90:83];diag_data[319:288]<=m[100:91];diag_data[351:320]<=m[132:101];
    diag_data[383:352]<=m[82:71];diag_data[415:384]<=peak_q12;
    diag_data[447:416]<={(cv?8'd1:8'd0),((final_reason==0&&row_local>=16)?8'd6:8'd1),((final_reason==0&&row_local>=16)?8'd6:8'd1),1'b0,final_reason};
    diag_data[479:448]<=32'hffffffff;diag_data[511:480]<={m[140:133],24'b0};diag_valid<=1;
    if(final_reason==0)begin
     qualified_count<=qualified_count+1;row_local<=row_local+1;
     // Internal descriptor extension: preserve the original low 78 bits and
     // use reserved bits [90:78] for signed refined V offset. Wire diagnostics
     // retain the unchanged packet 512-bit format.
     if(row_local<16)begin slot_we<=1;slot_addr<=m[70:62]*16+row_local;slot_data<={5'b0,dv,m[90:83],peak_q12,10'(m[51:42]-m[41:32]+1),uq,m[17:0]};end
     else overflow<=1;
    end
   end
   case(state)
    SCAN:if(fv&&fr)begin
     current_y<=fy;
     if(raw>row_peak)row_peak<=raw;
     if(raw>=250)begin row_sat250<=row_sat250+1;sat250_count<=sat250_count+1;end
     if(raw==255)begin row_sat255<=row_sat255+1;sat255_count<=sat255_count+1;end
     if(hit)begin
      if(!active)begin
       active<=1;first<=fx;last<=fx;best_x<=fx;best_y<=fy;best_strength<=strength;
       best_gx<=gx;best_gy<=gy;best_xx<=xx;best_xy<=xy;best_yy<=yy;
       retry_x<=fx;retry_smooth<=smooth;retry_strength<=strength;
       retry_gx<=gx;retry_gy<=gy;retry_xx<=xx;retry_xy<=xy;retry_yy<=yy;
       peak_raw<=raw;sat_run<=sats!=0;sum_strength<=strength;
       prev_strength<=strength;plateau_x<=fx;rising<=1;peak_count<=0;have_peak<=0;valley<=strength;retained_peak<=0;retained_x<=0;
      end else begin
       last<=fx;sum_strength<=sum_strength+strength;
       if(smooth>retry_smooth)begin
        retry_x<=fx;retry_smooth<=smooth;retry_strength<=strength;
        retry_gx<=gx;retry_gy<=gy;retry_xx<=xx;retry_xy<=xy;retry_yy<=yy;
       end
       if(strength>best_strength)begin best_x<=fx;best_y<=fy;best_strength<=strength;best_gx<=gx;best_gy<=gy;best_xx<=xx;best_xy<=xy;best_yy<=yy;end
       if(raw>peak_raw)peak_raw<=raw;if(sats!=0)sat_run<=sat_run+1;
       if(strength!=prev_strength)begin rising<=strength>prev_strength;plateau_x<=fx;end
       peak_count<=next_peak_count;have_peak<=next_have;retained_peak<=next_retained_peak;retained_x<=next_retained_x;valley<=next_valley;prev_strength<=strength;
      end
     end else if(active)begin active<=0;peak_count<=next_peak_count;state<=CLOSE;row_pending<=fx==WLR-1;end
     if(fx==WLR-1&&!active)begin row_pending<=0;state<=DRAIN;end
    end
    CLOSE:if(enqueue)begin
     retry_metadata[tail]<={retry_strength,retry_x,retry_yy,retry_xy,retry_xx,retry_gy,retry_gx};
     metadata[tail]<={best_yy,best_xy,best_xx,best_gy,best_gx,local_reason,peak_count,sum_strength,sat_run,peak_raw,best_strength,best_y,best_x,last,first,raw_count};
     completed[tail]<=0;tail<=tail+1;raw_count<=raw_count+1;row_raw<=row_raw+1;
     if(local_reason[2])border_seen<=1;
     state<=row_pending?DRAIN:SCAN;
    end
    DRAIN:if(occupancy==0&&!diag_valid)state<=ROW;
    ROW:begin
     row_we<=1;row_addr<=current_y;row_data<=0;row_data[4:0]<=row_local>16?16:row_local;row_data[8]<=row_local>16;
     row_data[63:32]<=row_peak;row_data[95:64]<=row_sat250;row_data[127:96]<=row_sat255;row_data[159:128]<=row_raw;row_data[191:160]<=row_local;
     if(current_y==H-1)begin finished<=1;state<=FIN;end
     else begin state<=SCAN;row_raw<=0;row_local<=0;row_sat250<=0;row_sat255<=0;row_peak<=0;row_pending<=0;end
    end
    FIN:finished<=1;
    default:state<=FIN;
   endcase
  end
 end
endmodule

// Versioned, bounded same-run seed retry. Original legal results are returned
// unchanged. A failed alternate also returns the original diagnostic exactly.
// One existing direction engine is reused, not a second arithmetic pipeline.
module direction_retry(
 input wire clk,rst,clear,start,input wire [59:0] features,alternate,
 input wire eligible,output wire ready,output reg done,center_valid,retry_used,
 output reg signed [12:0] du_q12,dv_q12,
 output reg signed [11:0] normal_x,normal_y
);
 localparam IDLE=0,FIRST=1,RETRY=2;
 reg [1:0] state;
 reg launch,allow_retry;
 reg [59:0] request,alternate_saved;
 reg [49:0] original_result;
 wire core_ready,core_done,core_valid;
 wire signed [12:0] core_du,core_dv;
 wire signed [11:0] core_nx,core_ny;
 assign ready=state==IDLE&&core_ready;
 direction_refiner solver(.clk(clk),.rst(rst),.clear(clear),.start(launch),
  .gx($signed(request[11:0])),.gy($signed(request[23:12])),
  .hxx($signed(request[35:24])),.hxy($signed(request[47:36])),.hyy($signed(request[59:48])),
  .ready(core_ready),.done(core_done),.center_valid(core_valid),
  .du_q12(core_du),.dv_q12(core_dv),.normal_x(core_nx),.normal_y(core_ny));
 always @(posedge clk)begin
  launch<=0;done<=0;
  if(rst||clear)begin
   state<=IDLE;launch<=0;done<=0;center_valid<=0;retry_used<=0;
   request<=0;alternate_saved<=0;allow_retry<=0;original_result<=0;
   du_q12<=0;dv_q12<=0;normal_x<=0;normal_y<=0;
  end else case(state)
   IDLE:if(start&&ready)begin
    request<=features;alternate_saved<=alternate;allow_retry<=eligible;
    launch<=1;state<=FIRST;
   end
   FIRST:if(core_done)begin
    if(!core_valid&&allow_retry)begin
     original_result<={core_ny,core_nx,core_dv,core_du};
     request<=alternate_saved;launch<=1;state<=RETRY;
    end else begin
     center_valid<=core_valid;retry_used<=0;
     {normal_y,normal_x,dv_q12,du_q12}<={core_ny,core_nx,core_dv,core_du};
     done<=1;state<=IDLE;
    end
   end
   RETRY:if(core_done)begin
    center_valid<=core_valid;retry_used<=core_valid;
    {normal_y,normal_x,dv_q12,du_q12}<=core_valid?{core_ny,core_nx,core_dv,core_du}:original_result;
    done<=1;state<=IDLE;
   end
   default:state<=IDLE;
  endcase
 end
endmodule
