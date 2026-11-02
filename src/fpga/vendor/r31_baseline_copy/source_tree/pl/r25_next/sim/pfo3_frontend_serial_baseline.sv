`timescale 1ns/1ps
// Fully bounded framed pixel frontend. The graph is computed in PL from compact
// row-qualified descriptors; the host never links candidates or scans pixels.
module pfo3_frontend_serial_baseline #(parameter FRAME_WIDTH=752,FRAME_HEIGHT=480)(
`include "pfo3_frontend_ports.svh"
);
 localparam N=FRAME_HEIGHT*16;
 localparam IDLE=0,INGRESS=1,INIT=2,PAIR_ADDR=3,PAIR_WAIT=4,PAIR_APPLY=5,
 LABEL_ADDR=6,LABEL_WAIT=7,LABEL_PRED=8,LABEL_PWAIT=9,LABEL_ROOT=10,
 LABEL_RWAIT=11,LABEL_WRITE=12,OUT_ADDR=13,OUT_WAIT=14,OUT_ROOT=15,
 OUT_RWAIT=16,OUT_BUILD=17,OUT_SEND=18,ROW_SEND=19,HIST=20,FINISH=21,PAIR_LATCH=22,HIST_READ=23;
 reg [5:0] state;
 reg clear_row;
 wire row_ready,row_done,row_slot_we,row_we,local_overflow,border_seen;
 wire [511:0] local_diag;
 wire local_valid;
 wire [12:0] row_slot_addr;
 wire [95:0] row_slot_data;
 wire [8:0] row_index;
 wire [191:0] row_data;
 wire [31:0] local_raw,local_qualified,local_sat250,local_sat255;
 (* ram_style="block" *) reg [95:0] descriptors[0:N-1];
 (* ram_style="distributed" *) reg [191:0] rows[0:FRAME_HEIGHT-1];
 reg [12:0] addr_a,addr_b,node,root_id;
 reg [95:0] da,db,held_desc;
 wire [4:0] pca,pcb,sca,scb;
 wire [12:0] pra,prb,la,lb;
 wire [8:0] sua,sub;
 wire ama,amb;
 wire [14:0] sta,stb;
 reg [1:0] pass;
 reg [8:0] pair_y,out_y;
 reg [3:0] pair_a,pair_b,out_slot;
 reg [511:0] out_record,row_record;
 reg [1:0] out_kind;
 reg [31:0] row_m,row_p,row_mw,row_pw,row_mp,row_pp;
 reg row_amb;
 reg [31:0] slotted_count;
 reg [12:0] hp_m[0:255],hp_p[0:255],hw_m[0:255],hw_p[0:255],hp_raw[0:255];
 reg [8:0] hist_i;
 reg [31:0] cm,cp,wm,wp,cr;
 reg [12:0] hm,hp,whm,whp,hr;
 reg [7:0] mplo,mphi,pplo,pphi,mwlo,mwhi,pwlo,pwhi,rlo,rhi;
 reg [31:0] amb_rows;
 reg low_support_seen;
 reg [31:0] delta_c,delta_w;
 reg edge_ok,eligible_a,eligible_b,eligible_node;
 reg edge_reg;
 reg [14:0] edge_delta;
 reg can_p,can_m;
 reg [7:0] reason_m,reason_p;
 reg [12:0] canonical;
 integer k;
 wire init_mem=state==INIT&&node<N;
 wire [12:0] ma=init_mem?node:addr_a;
 wire pair_edge=state==PAIR_APPLY&&edge_reg;
 wire count_edge=pair_edge&&pass==0;
 wire bad_edge=pair_edge&&pass==1&&(sca!=1||pcb!=1);
 wire stable_edge=pair_edge&&pass==2&&!ama&&!amb;
 pfo3_tdp_ram #(.WIDTH(5),.DEPTH(N)) pc_ram(clk,ma,addr_b,init_mem,count_edge,5'b0,pcb+5'd1,pca,pcb);
 pfo3_tdp_ram #(.WIDTH(5),.DEPTH(N)) sc_ram(clk,ma,addr_b,init_mem||count_edge,1'b0,init_mem?5'b0:sca+5'd1,5'b0,sca,scb);
 pfo3_tdp_ram #(.WIDTH(13),.DEPTH(N)) pred_ram(clk,ma,addr_b,init_mem,count_edge,13'b0,addr_a,pra,prb);
 pfo3_tdp_ram #(.WIDTH(13),.DEPTH(N)) label_ram(clk,ma,addr_b,init_mem||state==LABEL_WRITE,1'b0,init_mem?13'h1fff:root_id,13'b0,la,lb);
 pfo3_tdp_ram #(.WIDTH(9),.DEPTH(N)) support_ram(clk,ma,addr_b,init_mem,state==LABEL_WRITE,9'b0,sub+9'd1,sua,sub);
 pfo3_tdp_ram #(.WIDTH(1),.DEPTH(N)) amb_ram(clk,ma,addr_b,init_mem||bad_edge,bad_edge,!init_mem,1'b1,ama,amb);
 pfo3_tdp_ram #(.WIDTH(15),.DEPTH(N)) stability_ram(clk,ma,addr_b,init_mem||(stable_edge&&edge_delta>sta),stable_edge&&edge_delta>stb,init_mem?15'b0:edge_delta,edge_delta,sta,stb);
 pfo3_row_r25 #(.W(FRAME_WIDTH),.H(FRAME_HEIGHT)) row_engine(
  .clk(clk),.rst(rst),.clear(clear_row),.pixel_data(pixel_data),
  .pixel_valid(pixel_valid&&busy&&state==INGRESS&&!clear_row),.pixel_ready(row_ready),
  .diag_data(local_diag),.diag_valid(local_valid),.diag_ready(record_ready&&state==INGRESS),
  .slot_we(row_slot_we),.slot_addr(row_slot_addr),.slot_data(row_slot_data),
  .row_we(row_we),.row_addr(row_index),.row_data(row_data),.finished(row_done),
  .raw_count(local_raw),.qualified_count(local_qualified),.sat250_count(local_sat250),
  .sat255_count(local_sat255),.overflow(local_overflow),.border_seen(border_seen));
 assign pixel_ready=busy&&state==INGRESS&&row_ready&&!abort&&!clear_row;
 assign record_data=state==INGRESS?local_diag:out_record;
 assign record_kind=state==INGRESS?2'd0:out_kind;
 assign record_valid=!abort&&!clear_row&&busy&&((state==INGRESS&&local_valid)||state==OUT_SEND||state==ROW_SEND);
 always @(posedge clk)begin
  da<=descriptors[addr_a];db<=descriptors[addr_b];
  if(row_slot_we)descriptors[row_slot_addr]<=row_slot_data;
  if(row_we)rows[row_index]<=row_data;
 end
 always @*begin
  delta_c=(da[39:18]>db[39:18])?da[39:18]-db[39:18]:db[39:18]-da[39:18];
  delta_w=(da[49:40]>db[49:40])?da[49:40]-db[49:40]:db[49:40]-da[49:40];
  eligible_a=pair_a<rows[pair_y-1][4:0]&&!rows[pair_y-1][8];
  eligible_b=pair_b<rows[pair_y][4:0]&&!rows[pair_y][8];
  edge_ok=eligible_a&&eligible_b&&delta_c<=24576&&delta_w<=8;
  eligible_node=(node[3:0]<rows[node>>4][4:0])&&!rows[node>>4][8];
  can_p=eligible_node&&!ama&&!local_overflow&&sub>=3;
  can_m=can_p&&sub>=8&&held_desc[69:50]>=73728;
  reason_p=0;reason_m=0;
  if(local_overflow)begin reason_p=6;reason_m=6;end
  else if(ama)begin reason_p=2;reason_m=2;end
  else begin
   if(sub<3)reason_p=3;
   if(sub<8)reason_m=3;else if(held_desc[69:50]<73728)reason_m=5;
  end
  canonical=(la==13'h1fff)?13'h1fff:la;
 end
 always @(posedge clk)begin
  clear_row<=0;done<=0;
  if(rst)begin
   state<=IDLE;busy<=0;done<=0;error<=0;error_code<=0;flags<=0;
   diagnostic_count<=0;qualified_count<=0;measurement_count<=0;preview_count<=0;
   measurement_rows<=0;preview_rows<=0;ambiguous_count<=0;sat250_count<=0;sat255_count<=0;
   processing_cycles<=0;input_bytes<=0;measure_status<=5;preview_status<=5;
   measure_median_peak_x2<=0;measure_median_width_x2<=0;
   preview_median_peak_x2<=0;preview_median_width_x2<=0;raw_peak_median_x2<=0;
   frame_measurement_usable<=0;frame_preview_usable<=0;frame_quality_flags<=0;ambiguous_rows<=0;
   addr_a<=0;addr_b<=0;node<=0;pair_y<=1;pair_a<=0;pair_b<=0;
   out_y<=0;out_slot<=0;out_record<=0;out_kind<=0;hist_i<=0;
   edge_reg<=0;edge_delta<=0;
   hm<=0;hp<=0;whm<=0;whp<=0;hr<=0;
  end else if(abort&&busy)begin
   busy<=0;done<=1;error<=1;error_code<=2;flags<=16;state<=IDLE;clear_row<=1;
   frame_measurement_usable<=0;frame_preview_usable<=0;
  end else begin
   if(busy)processing_cycles<=processing_cycles+1;
   if(pixel_valid&&pixel_ready)begin
    input_bytes<=input_bytes+1;
    if(pixel_sof!=(input_bytes==0)||pixel_eof!=(input_bytes==FRAME_WIDTH*FRAME_HEIGHT-1))begin
     busy<=0;done<=1;error<=1;error_code<=1;flags<=16;state<=IDLE;clear_row<=1;
     frame_measurement_usable<=0;frame_preview_usable<=0;
    end
   end
   case(state)
    IDLE:if(start)begin
     clear_row<=1;busy<=1;error<=0;error_code<=0;flags<=0;state<=INGRESS;
     input_bytes<=0;processing_cycles<=0;measurement_count<=0;preview_count<=0;
     measurement_rows<=0;preview_rows<=0;ambiguous_count<=0;slotted_count<=0;amb_rows<=0;
     low_support_seen<=0;
     frame_measurement_usable<=0;frame_preview_usable<=0;frame_quality_flags<=0;ambiguous_rows<=0;
     row_m<=0;row_p<=0;row_mw<=0;row_pw<=0;row_mp<=0;row_pp<=0;row_amb<=0;
    end
    INGRESS:if(row_done&&!clear_row)begin
     diagnostic_count<=local_raw;qualified_count<=local_qualified;
     sat250_count<=local_sat250;sat255_count<=local_sat255;
     flags<=local_overflow?10:0;node<=0;state<=INIT;hist_i<=0;
    end
    INIT:begin
     if(node<256)begin hp_m[node]<=0;hp_p[node]<=0;hw_m[node]<=0;hw_p[node]<=0;hp_raw[node]<=0;end
     if(node==((N>256?N:256)-1))begin pass<=0;pair_y<=1;pair_a<=0;pair_b<=0;
      if(FRAME_HEIGHT==1)begin node<=0;state<=LABEL_ADDR;end else state<=PAIR_ADDR;
     end else node<=node+1;
    end
    PAIR_ADDR:begin addr_a<=(pair_y-1)*16+pair_a;addr_b<=pair_y*16+pair_b;state<=PAIR_WAIT;end
    PAIR_WAIT:state<=PAIR_LATCH;
    PAIR_LATCH:begin edge_reg<=edge_ok;edge_delta<=delta_c;state<=PAIR_APPLY;end
    PAIR_APPLY:begin
     if(pair_b==15)begin pair_b<=0;
      if(pair_a==15)begin pair_a<=0;
       if(pair_y==FRAME_HEIGHT-1)begin
        pair_y<=1;
        if(pass==2)begin node<=0;state<=LABEL_ADDR;end
        else begin pass<=pass+1;state<=PAIR_ADDR;end
       end else begin pair_y<=pair_y+1;state<=PAIR_ADDR;end
      end else begin pair_a<=pair_a+1;state<=PAIR_ADDR;end
     end else begin pair_b<=pair_b+1;state<=PAIR_ADDR;end
    end
    LABEL_ADDR:begin addr_a<=node;state<=LABEL_WAIT;end
    LABEL_WAIT:state<=LABEL_PRED;
    LABEL_PRED:begin
     if(eligible_node&&!ama)begin
      if(pca==1)begin addr_b<=pra;state<=LABEL_PWAIT;end
      else begin root_id<=node;addr_b<=node;state<=LABEL_RWAIT;end
     end else begin
      if(node==N-1)begin node<=0;out_y<=0;out_slot<=0;state<=OUT_ADDR;end
      else begin node<=node+1;state<=LABEL_ADDR;end
     end
    end
    LABEL_PWAIT:state<=LABEL_ROOT;
    LABEL_ROOT:begin
     if(!amb&&lb!=13'h1fff)begin root_id<=lb;addr_b<=lb;end
     else begin root_id<=node;addr_b<=node;end
     state<=LABEL_RWAIT;
    end
    LABEL_RWAIT:state<=LABEL_WRITE;
    LABEL_WRITE:begin
     if(node==N-1)begin node<=0;out_y<=0;out_slot<=0;state<=OUT_ADDR;end
     else begin node<=node+1;state<=LABEL_ADDR;end
    end
    OUT_ADDR:begin addr_a<=node;state<=OUT_WAIT;end
    OUT_WAIT:state<=OUT_ROOT;
    OUT_ROOT:begin
     held_desc<=da;
     if(node[3:0]<rows[out_y][4:0])begin
      slotted_count<=slotted_count+1;
      if(ama)begin ambiguous_count<=ambiguous_count+1;row_amb<=1;end
      addr_b<=la;state<=OUT_RWAIT;
     end else begin
      if(node[3:0]==15)begin state<=ROW_SEND;out_kind<=2;out_record<=0;
       out_record[31:0]<=out_y;out_record[63:32]<=rows[out_y][63:32];
       out_record[95:64]<=rows[out_y][95:64];out_record[127:96]<=rows[out_y][127:96];
       out_record[159:128]<=rows[out_y][159:128];out_record[191:160]<=rows[out_y][191:160];
       out_record[223:192]<=row_m;out_record[255:224]<=row_p;out_record[287:256]<=row_mw;
       out_record[319:288]<=row_pw;out_record[351:320]<=row_mp;out_record[383:352]<=row_pp;
       out_record[415:384]<=row_amb;out_record[447:416]<=row_amb;
      end else begin node<=node+1;state<=OUT_ADDR;end
     end
    end
    OUT_RWAIT:state<=OUT_BUILD;
    OUT_BUILD:begin
     if(eligible_node&&!ama&&sub<3)low_support_seen<=1;
     out_record<=0;out_kind<=1;
     out_record[31:0]<=held_desc[17:0];
     out_record[447:416]<={5'b0,can_p,can_m,1'b1,reason_p,reason_m,8'b0};
     out_record[479:448]<={(can_p?{3'b0,la}:16'hffff),(can_m?{3'b0,la}:16'hffff)};
     out_record[503:480]<=sta;
     if(can_m)begin
      measurement_count<=measurement_count+1;row_m<=row_m+1;
      if(held_desc[49:40]>row_mw)row_mw<=held_desc[49:40];
      if(held_desc[77:70]>row_mp)row_mp<=held_desc[77:70];
      hp_m[held_desc[77:70]]<=hp_m[held_desc[77:70]]+1;
      hw_m[held_desc[49:40]]<=hw_m[held_desc[49:40]]+1;
     end
     if(can_p)begin
      preview_count<=preview_count+1;row_p<=row_p+1;
      if(held_desc[49:40]>row_pw)row_pw<=held_desc[49:40];
      if(held_desc[77:70]>row_pp)row_pp<=held_desc[77:70];
      hp_p[held_desc[77:70]]<=hp_p[held_desc[77:70]]+1;
      hw_p[held_desc[49:40]]<=hw_p[held_desc[49:40]]+1;
     end
     state<=OUT_SEND;
    end
    OUT_SEND:if(record_ready)begin
     if(node[3:0]==15)begin state<=ROW_SEND;out_kind<=2;out_record<=0;
      out_record[31:0]<=out_y;out_record[63:32]<=rows[out_y][63:32];
      out_record[95:64]<=rows[out_y][95:64];out_record[127:96]<=rows[out_y][127:96];
      out_record[159:128]<=rows[out_y][159:128];out_record[191:160]<=rows[out_y][191:160];
      out_record[223:192]<=row_m;out_record[255:224]<=row_p;out_record[287:256]<=row_mw;
      out_record[319:288]<=row_pw;out_record[351:320]<=row_mp;out_record[383:352]<=row_pp;
      out_record[415:384]<=row_amb;out_record[447:416]<=row_amb;
     end else begin node<=node+1;state<=OUT_ADDR;end
    end
    ROW_SEND:if(record_ready)begin
     if(row_m!=0)measurement_rows<=measurement_rows+1;
     if(row_p!=0)preview_rows<=preview_rows+1;
     if(row_amb)amb_rows<=amb_rows+1;
     hp_raw[rows[out_y][39:32]]<=hp_raw[rows[out_y][39:32]]+1;
     row_m<=0;row_p<=0;row_mw<=0;row_pw<=0;row_mp<=0;row_pp<=0;row_amb<=0;
     if(out_y==FRAME_HEIGHT-1)begin hist_i<=0;cm<=0;cp<=0;wm<=0;wp<=0;cr<=0;
      mplo<=0;mphi<=0;pplo<=0;pphi<=0;mwlo<=0;mwhi<=0;pwlo<=0;pwhi<=0;rlo<=0;rhi<=0;
      state<=HIST_READ;
     end else begin out_y<=out_y+1;node<=node+1;state<=OUT_ADDR;end
    end
    HIST_READ:begin
     hm<=hp_m[hist_i];hp<=hp_p[hist_i];whm<=hw_m[hist_i];whp<=hw_p[hist_i];hr<=hp_raw[hist_i];
     state<=HIST;
    end
    HIST:begin
     if(cm<=((measurement_count-1)>>1)&&cm+hm>((measurement_count-1)>>1))mplo<=hist_i;
     if(cm<=(measurement_count>>1)&&cm+hm>(measurement_count>>1))mphi<=hist_i;
     if(cp<=((preview_count-1)>>1)&&cp+hp>((preview_count-1)>>1))pplo<=hist_i;
     if(cp<=(preview_count>>1)&&cp+hp>(preview_count>>1))pphi<=hist_i;
     if(wm<=((measurement_count-1)>>1)&&wm+whm>((measurement_count-1)>>1))mwlo<=hist_i;
     if(wm<=(measurement_count>>1)&&wm+whm>(measurement_count>>1))mwhi<=hist_i;
     if(wp<=((preview_count-1)>>1)&&wp+whp>((preview_count-1)>>1))pwlo<=hist_i;
     if(wp<=(preview_count>>1)&&wp+whp>(preview_count>>1))pwhi<=hist_i;
     if(cr<=((FRAME_HEIGHT-1)>>1)&&cr+hr>((FRAME_HEIGHT-1)>>1))rlo<=hist_i;
     if(cr<=(FRAME_HEIGHT>>1)&&cr+hr>(FRAME_HEIGHT>>1))rhi<=hist_i;
     cm<=cm+hm;cp<=cp+hp;wm<=wm+whm;wp<=wp+whp;cr<=cr+hr;
     if(hist_i==255)state<=FINISH;else begin hist_i<=hist_i+1;state<=HIST_READ;end
    end
    FINISH:begin
     frame_preview_usable<=preview_count!=0;
     frame_measurement_usable<=measurement_count>=8&&measurement_rows*4>=FRAME_HEIGHT&&!local_overflow&&
       sat250_count*1000<=FRAME_WIDTH*FRAME_HEIGHT&&ambiguous_count*10<=slotted_count;
     frame_quality_flags<=(preview_count==0?1:0)|
       ((measurement_count>=8&&measurement_rows*4>=FRAME_HEIGHT&&!local_overflow&&sat250_count*1000<=FRAME_WIDTH*FRAME_HEIGHT&&ambiguous_count*10<=slotted_count)?0:2)|
       (local_overflow?4:0)|(ambiguous_count!=0?8:0)|(sat250_count!=0?16:0)|(border_seen?32:0)|(low_support_seen?64:0);
     ambiguous_rows<=amb_rows;
     measure_median_peak_x2<=mplo+mphi;measure_median_width_x2<=mwlo+mwhi;
     preview_median_peak_x2<=pplo+pphi;preview_median_width_x2<=pwlo+pwhi;raw_peak_median_x2<=rlo+rhi;
     if(sat250_count*1000>FRAME_WIDTH*FRAME_HEIGHT)begin measure_status<=1;preview_status<=1;end
     else if(local_overflow||ambiguous_count*10>slotted_count)begin measure_status<=2;preview_status<=2;end
     else begin
      measure_status<=measurement_count==0?3:((measurement_count>=8&&measurement_rows*4>=FRAME_HEIGHT)?0:4);
      preview_status<=preview_count==0?3:0;
     end
     flags<=flags|1;busy<=0;done<=1;state<=IDLE;
    end
    default:state<=IDLE;
   endcase
  end
 end
endmodule

