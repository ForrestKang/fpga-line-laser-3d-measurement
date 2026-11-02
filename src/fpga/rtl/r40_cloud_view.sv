`timescale 1ns/1ps
// R40 PL point-cloud view. Contract: model/r40_model.py (bit-exact).
//
// aclk (100 MHz, PFO3 domain)
//   snoop    read-only copy of accepted geometry records; never back-pressures PFO3
//   ingest   quantize axis XYZ Q16 -> voxel address -> staging FIFO; a packet that ends
//            without error commits its points, any other end discards them
//   drain    committed addresses set bits in the 64x64x96 occupancy grid
//   render   back-to-front orthographic splat into the back half of a 2x224x240 panel
//   publish  xpm_cdc_handshake {overlay_en,wx,wy,buffer} to the video side, then wait
//            for the swap toggle before the published buffer is ever written again
// vclk (125 MHz, VDMA MM2S domain)
//   overlay  5-stage pass-through of the BGRX stream; inside the 448x480 window a
//            non-zero panel pixel (2x upscaled) replaces RGB with the palette colour.
//            Buffer swaps happen only on start-of-frame. Reset state is pure bypass.
// DSP48 use is requested only on the multiplier registers (USE_DSP on signals);
// adders, comparators and address arithmetic stay in fabric (b02 timing lesson).
module r40_cloud_view(
 input wire aclk,aresetn,
 input wire geo_fire,input wire [255:0] geo_record,
 input wire pkt_commit,pkt_discard,
 input wire reg_we,input wire [7:0] reg_waddr,input wire [31:0] reg_wdata,
 input wire [7:0] reg_raddr,output reg [31:0] reg_rdata,
 input wire vclk,vresetn,
 input wire [31:0] s_video_tdata,input wire s_video_tvalid,output wire s_video_tready,
 input wire s_video_tlast,s_video_tuser,
 output reg [31:0] m_video_tdata,output reg m_video_tvalid,input wire m_video_tready,
 output reg m_video_tlast,m_video_tuser
);
 localparam PW=224,PH=240,PANEL=53760,WIN_W=448,WIN_H=480,VWORDS=12288;
 localparam [31:0] MAGIC=32'h52343056;

 // ---------------------------------------------------------------- registers
 reg overlay_en,insert_en,auto_rotate,use_preview,freeze;
 reg [17:0] inv;
 reg signed [31:0] ox,oy,oz;
 reg [9:0] yaw_fixed,yaw_step;
 reg signed [17:0] k,kse,kce;
 reg signed [10:0] sx0,sy0;
 reg [7:0] zc;
 reg [10:0] wx,wy;
 reg [2:0] splat;
 reg [31:0] rb_addr,rb_data;
 reg rb_req,rb_valid,clear_req;
 reg [31:0] inserted,dropped,frames_committed,frames_discarded,occupied,out_of_range;
 reg [31:0] render_count,last_render_cycles,swap_count;
 reg clear_busy,last_pub_buf;
 reg [9:0] yaw_cur,last_yaw;
 reg [3:0] r_state;
 localparam R_START=0,R_COEF=1,R_CLEAR=2,R_RD=3,R_RW1=4,R_RW2=5,R_WORD=6,R_BIT=7,
            R_P1=8,R_P2=9,R_P3=10,R_P4=11,R_SPL=12,R_NEXT=13,R_PUB=14,R_FROZEN=15;
 reg r_pub_wait,r_pend_swap;
 // A publish whose handshake pulse lands while the video side is in reset (HDMI not started yet,
 // e.g. right after the bitstream loads) is lost and no swap ever comes. Re-send the same
 // publish when no swap arrived for PUB_RETRY cycles (~42 ms, > 2 frames). The video side keeps
 // one pending flag, so a duplicate can only re-arm a swap that has not happened yet.
 localparam [21:0] PUB_RETRY=22'h3fffff;
 reg [21:0] pub_to;
 reg [31:0] republish_count;

 always @* begin
  case(reg_raddr)
   8'h00:reg_rdata={26'b0,freeze,use_preview,1'b0,auto_rotate,insert_en,overlay_en};
   8'h04:reg_rdata={14'b0,inv};
   8'h08:reg_rdata=ox;8'h0c:reg_rdata=oy;8'h10:reg_rdata=oz;
   8'h14:reg_rdata={6'b0,yaw_step,6'b0,yaw_fixed};
   8'h18:reg_rdata={{14{k[17]}},k};8'h1c:reg_rdata={{14{kse[17]}},kse};8'h20:reg_rdata={{14{kce[17]}},kce};
   8'h24:reg_rdata={5'b0,sy0,5'b0,sx0};
   8'h28:reg_rdata={24'b0,zc};
   8'h2c:reg_rdata={5'b0,wy,5'b0,wx};
   8'h30:reg_rdata={29'b0,splat};
   8'h34:reg_rdata=rb_addr;8'h38:reg_rdata=rb_data;
   8'h40:reg_rdata=inserted;8'h44:reg_rdata=dropped;
   8'h48:reg_rdata=frames_committed;8'h4c:reg_rdata=frames_discarded;
   8'h50:reg_rdata=occupied;8'h54:reg_rdata=out_of_range;
   8'h58:reg_rdata=render_count;8'h5c:reg_rdata=last_render_cycles;8'h60:reg_rdata=swap_count;
   8'h64:reg_rdata={6'b0,yaw_cur,8'b0,r_state,rb_valid,last_pub_buf,r_state==R_FROZEN,clear_busy||clear_req};
   8'h68:reg_rdata=MAGIC;
   8'h6c:reg_rdata={22'b0,last_yaw};
   8'h70:reg_rdata=republish_count;
   default:reg_rdata=0;
  endcase
 end

 // ---------------------------------------------------------------- ingest
 // q1: select + capture; q2: subtract/range; q3: multiply (DSP M); q4: product (DSP P);
 // q5: index range check -> push. Packet ends travel the same depth to keep order.
 reg q1,q2,q3,q4,q5,q5in;
 reg [18:0] q5vaddr;
 reg signed [31:0] q1x,q1y,q1z;
 reg [23:0] q2dx,q2dy,q2dz;
 reg q2ok;
 (* use_dsp="yes" *) reg [40:0] q3px,q3py,q3pz;
 reg [40:0] q4px,q4py,q4pz;
 reg q3ok,q4ok;
 reg [4:0] commit_d,discard_d;
 wire signed [32:0] dx33=$signed({q1x[31],q1x})-$signed({ox[31],ox});
 wire signed [32:0] dy33=$signed({q1y[31],q1y})-$signed({oy[31],oy});
 wire signed [32:0] dz33=$signed({q1z[31],q1z})-$signed({oz[31],oz});
 wire in24x=!dx33[32]&&dx33[31:24]==0,in24y=!dy33[32]&&dy33[31:24]==0,in24z=!dz33[32]&&dz33[31:24]==0;
 wire [8:0] ix9=q4px[40:32],iy9=q4py[40:32],iz9=q4pz[40:32];
 wire idx_ok=ix9<64&&iy9<64&&iz9<96;
 wire [18:0] vaddr_new={iz9[6:0],iy9[5:0],ix9[5:0]};
 reg [11:0] wr_ptr,cm_ptr,rd_ptr;
 wire [11:0] fill=wr_ptr-rd_ptr;
 wire push=q5&&q5in&&fill!=12'd2048;
 wire [1:0] oor_inc=(q4&&!q4ok)+(q5&&!q5in);
 wire [18:0] stage_q;
 reg [10:0] stage_raddr;
 wire mode_ok=use_preview?geo_record[225]:geo_record[224];

 // ---------------------------------------------------------------- drain / clear / voxel readback (port B)
 reg [2:0] d_state;
 localparam D_IDLE=0,D_S1=1,D_S2=2,D_W1=3,D_W2=4,D_W3=5,D_CLR=6,D_RB=7;
 reg [13:0] vb_addr;reg vb_we;reg [31:0] vb_d;wire [31:0] vb_q;
 reg [18:0] d_vaddr;reg [1:0] d_wait;
 reg [13:0] clr_addr;

 // ---------------------------------------------------------------- renderer
 wire signed [15:0] sin_v,cos_v;
 r40_sin_rom sin_rom(.i(yaw_cur),.v(sin_v));
 r40_sin_rom cos_rom(.i(yaw_cur+10'd256),.v(cos_v));
 reg signed [15:0] rs,rc;
 wire signed [16:0] nrs=-rs,nrc=-rc;          // |sin| <= 16384, so 17 bits are exact
 wire signed [16:0] prc=rc;
 (* use_dsp="yes" *) reg signed [34:0] pk1,pk2,pk3,pk4;
 reg signed [17:0] ca,cb,cc,cd,ce;
 reg xasc,yasc,zasc;
 reg [1:0] coef_step;
 reg rend_buf;
 reg [16:0] pix;
 reg [6:0] iz;reg [5:0] iy;reg h;
 reg [13:0] va_addr;wire [31:0] va_q;
 reg [31:0] word;
 reg [5:0] ix;
 reg signed [7:0] X,Y;reg signed [8:0] Z;
 (* use_dsp="yes" *) reg signed [26:0] m1,m2,m3,m4,m5;
 reg signed [27:0] sxs,sys;
 reg signed [16:0] sx,sy;
 reg [3:0] col;
 reg signed [2:0] dxs,dys;
 reg [31:0] cyc;
 reg ca_we;reg [16:0] ca_addr;reg [3:0] ca_d;wire [3:0] ca_q;
 reg [1:0] rb_wait;
 wire [2:0] nsp=(splat==0) ? 3'd1 : ((splat>3'd4) ? 3'd4 : splat);
 wire signed [2:0] lo=-$signed({1'b0,(nsp-3'd1)>>1});
 wire signed [2:0] hi=lo+$signed({1'b0,nsp})-4'sd1;
 reg signed [2:0] lo_r,hi_r;                  // splat is static: register its range
 wire [9:0] iz5=iz*10'd5;
 wire signed [17:0] px=sx+dxs,py=sy+dys;
 wire pin=px>=0&&px<PW&&py>=0&&py<PH;
 wire [16:0] pbase=rend_buf?17'd53760:17'd0;
 wire [16:0] paddr=pbase+{py[8:0],8'b0}-{py[11:0],5'b0}+px[8:0];

 function automatic [4:0] lowest(input [31:0] w);
  integer n;begin lowest=0;for(n=31;n>=0;n=n-1)if(w[n])lowest=n[4:0];end
 endfunction
 function automatic [4:0] highest(input [31:0] w);
  integer n;begin highest=0;for(n=0;n<32;n=n+1)if(w[n])highest=n[4:0];end
 endfunction
 wire [4:0] bitpos=xasc?lowest(word):highest(word);
 wire last_word=(xasc?h==1:h==0)&&(yasc?iy==63:iy==0)&&(zasc?iz==95:iz==0);

 // publish handshake (aclk -> vclk) and swap toggle (vclk -> aclk)
 reg hs_send;reg [23:0] hs_data;wire hs_rcv;
 wire [23:0] hs_dest;wire hs_req;
 reg swap_tgl;wire swap_sync;reg swap_seen;

 always @(posedge aclk)begin
  if(!aresetn)begin
   overlay_en<=0;insert_en<=0;auto_rotate<=0;use_preview<=0;freeze<=0;
   inv<=18'd87381;ox<=-32'sd1572864;oy<=-32'sd1572864;oz<=-32'sd131072;
   yaw_fixed<=0;yaw_step<=10'd2;k<=18'sd6144;kse<=18'sd2401;kce<=18'sd5656;
   sx0<=11'sd112;sy0<=11'sd120;zc<=8'd69;wx<=11'd808;wy<=11'd128;splat<=3'd3;
   rb_addr<=0;rb_data<=0;rb_req<=0;rb_valid<=0;clear_req<=0;
   inserted<=0;dropped<=0;frames_committed<=0;frames_discarded<=0;occupied<=0;out_of_range<=0;
   render_count<=0;last_render_cycles<=0;swap_count<=0;clear_busy<=0;last_pub_buf<=0;
   q1<=0;q2<=0;q3<=0;q4<=0;q5<=0;q2ok<=0;q3ok<=0;q4ok<=0;commit_d<=0;discard_d<=0;
   wr_ptr<=0;cm_ptr<=0;rd_ptr<=0;stage_raddr<=0;
   d_state<=D_IDLE;vb_we<=0;vb_addr<=0;vb_d<=0;d_vaddr<=0;d_wait<=0;clr_addr<=0;
   r_state<=R_START;r_pend_swap<=0;r_pub_wait<=0;yaw_cur<=0;last_yaw<=0;coef_step<=0;rend_buf<=0;pix<=0;
   iz<=0;iy<=0;h<=0;va_addr<=0;word<=0;ix<=0;cyc<=0;ca_we<=0;ca_addr<=0;ca_d<=0;rb_wait<=0;
   hs_send<=0;hs_data<=0;swap_seen<=0;dxs<=0;dys<=0;pub_to<=0;republish_count<=0;
  end else begin
   // ---- register writes
   if(reg_we)begin
    case(reg_waddr)
     8'h00:begin overlay_en<=reg_wdata[0];insert_en<=reg_wdata[1];auto_rotate<=reg_wdata[2];
                 if(reg_wdata[3])clear_req<=1;use_preview<=reg_wdata[4];freeze<=reg_wdata[5];end
     8'h04:inv<=reg_wdata[17:0];
     8'h08:ox<=reg_wdata;8'h0c:oy<=reg_wdata;8'h10:oz<=reg_wdata;
     8'h14:begin yaw_fixed<=reg_wdata[9:0];yaw_step<=reg_wdata[25:16];end
     8'h18:k<=reg_wdata[17:0];8'h1c:kse<=reg_wdata[17:0];8'h20:kce<=reg_wdata[17:0];
     8'h24:begin sx0<=reg_wdata[10:0];sy0<=reg_wdata[26:16];end
     8'h28:zc<=reg_wdata[7:0];
     8'h2c:begin wx<=reg_wdata[10:0];wy<=reg_wdata[26:16];end
     8'h30:splat<=reg_wdata[2:0];
     8'h34:begin rb_addr<=reg_wdata;rb_req<=1;rb_valid<=0;end
     default:begin end
    endcase
   end
   lo_r<=lo;hi_r<=hi;
   // ---- ingest pipeline
   q1<=geo_fire&&insert_en&&geo_record[232]&&mode_ok;
   q1x<=geo_record[159:128];q1y<=geo_record[191:160];q1z<=geo_record[223:192];
   q2<=q1;q2ok<=in24x&&in24y&&in24z;q2dx<=dx33[23:0];q2dy<=dy33[23:0];q2dz<=dz33[23:0];
   q3<=q2;q3ok<=q2ok;q3px<=q2dx*inv;q3py<=q2dy*inv;q3pz<=q2dz*inv;
   q4<=q3;q4ok<=q3ok;q4px<=q3px;q4py<=q3py;q4pz<=q3pz;
   q5<=q4&&q4ok;q5in<=idx_ok;q5vaddr<=vaddr_new;
   if(oor_inc!=0)out_of_range<=out_of_range+oor_inc;
   commit_d<={commit_d[3:0],pkt_commit};discard_d<={discard_d[3:0],pkt_discard};
   if(q5&&q5in&&!push)dropped<=dropped+1;
   if(push)wr_ptr<=wr_ptr+1'b1;
   if(commit_d[4])begin cm_ptr<=push?wr_ptr+1'b1:wr_ptr;frames_committed<=frames_committed+1;end
   else if(discard_d[4])begin
    if(wr_ptr!=cm_ptr)frames_discarded<=frames_discarded+1;
    wr_ptr<=cm_ptr;
   end
   // ---- drain / clear / voxel readback on port B
   vb_we<=0;
   case(d_state)
    D_IDLE:begin
     if(clear_req)begin
      clear_req<=0;clear_busy<=1;clr_addr<=0;d_state<=D_CLR;
      wr_ptr<=0;cm_ptr<=0;rd_ptr<=0;
      inserted<=0;dropped<=0;frames_committed<=0;frames_discarded<=0;occupied<=0;out_of_range<=0;
     end else if(rd_ptr!=cm_ptr)begin stage_raddr<=rd_ptr[10:0];d_state<=D_S1;end
     else if(rb_req&&!rb_addr[31])begin vb_addr<=rb_addr[13:0];d_wait<=0;d_state<=D_RB;end
    end
    D_S1:d_state<=D_S2;                                    // stage RAM latency 1
    D_S2:begin d_vaddr<=stage_q;vb_addr<=stage_q[18:5];d_state<=D_W1;end
    D_W1:d_state<=D_W2;                                    // voxel RAM latency 2
    D_W2:d_state<=D_W3;
    D_W3:begin
     vb_we<=1;vb_d<=vb_q|(32'd1<<d_vaddr[4:0]);
     if(!vb_q[d_vaddr[4:0]])occupied<=occupied+1;
     inserted<=inserted+1;rd_ptr<=rd_ptr+1'b1;d_state<=D_IDLE;
    end
    D_CLR:begin
     vb_we<=1;vb_addr<=clr_addr;vb_d<=0;clr_addr<=clr_addr+1'b1;
     if(clr_addr==VWORDS-1)begin clear_busy<=0;d_state<=D_IDLE;end
    end
    D_RB:begin
     if(d_wait==2)begin rb_data<=vb_q;rb_valid<=1;rb_req<=0;d_state<=D_IDLE;end
     else d_wait<=d_wait+1'b1;
    end
    default:d_state<=D_IDLE;
   endcase
   // ---- renderer
   ca_we<=0;
   if(r_state!=R_FROZEN&&r_state!=R_START)cyc<=cyc+1;
   case(r_state)
    R_START:begin
     if(freeze)begin r_pend_swap<=0;r_state<=R_FROZEN;end
     else begin yaw_cur<=auto_rotate?yaw_cur:yaw_fixed;coef_step<=0;cyc<=0;r_state<=R_COEF;end
    end
    R_COEF:begin
     coef_step<=coef_step+1'b1;
     if(coef_step==0)begin rs<=sin_v;rc<=cos_v;end
     if(coef_step==1)begin pk1<=k*nrs;pk2<=k*prc;pk3<=kse*nrc;pk4<=kse*nrs;end
     if(coef_step==2)begin
      ca<=pk1>>>14;cb<=pk2>>>14;cc<=pk3>>>14;cd<=pk4>>>14;ce<=kce;
      xasc<=rc>=0;yasc<=rs>=0;zasc<=kse>=0;pix<=0;r_state<=R_CLEAR;
     end
    end
    R_CLEAR:begin
     ca_we<=1;ca_addr<=pbase+pix;ca_d<=0;pix<=pix+1'b1;
     if(pix==PANEL-1)begin
      iz<=zasc?7'd0:7'd95;iy<=yasc?6'd0:6'd63;h<=xasc?1'b0:1'b1;r_state<=R_RD;
     end
    end
    R_RD:begin va_addr<={iz,iy,h};r_state<=R_RW1;end
    R_RW1:r_state<=R_RW2;
    R_RW2:r_state<=R_WORD;
    R_WORD:begin word<=va_q;r_state<=va_q==0?R_NEXT:R_BIT;end
    R_BIT:begin
     ix<={h,bitpos};word[bitpos]<=1'b0;r_state<=R_P1;
    end
    R_P1:begin
     X<=$signed({1'b0,ix,1'b0})-8'sd63;Y<=$signed({1'b0,iy,1'b0})-8'sd63;
     Z<=$signed({1'b0,iz,1'b0})-$signed({1'b0,zc});
     col<=4'd1+iz5[9:5];r_state<=R_P2;
    end
    R_P2:begin m1<=ca*X;m2<=cb*Y;m3<=cc*X;m4<=cd*Y;m5<=ce*Z;r_state<=R_P3;end
    R_P3:begin sxs<=m1+m2+28'sd2048;sys<=m3+m4+m5+28'sd2048;r_state<=R_P4;end
    R_P4:begin
     sx<=sx0+(sxs>>>12);sy<=sy0-(sys>>>12);dxs<=lo_r;dys<=lo_r;r_state<=R_SPL;
    end
    R_SPL:begin
     if(pin)begin ca_we<=1;ca_addr<=paddr;ca_d<=col;end
     if(dxs==hi_r)begin
      dxs<=lo_r;
      if(dys==hi_r)r_state<=word!=0?R_BIT:R_NEXT;
      else dys<=dys+1'b1;
     end else dxs<=dxs+1'b1;
    end
    R_NEXT:begin
     if(last_word)begin
      hs_data<={overlay_en,wx,wy,rend_buf};hs_send<=1;r_pub_wait<=0;pub_to<=0;last_pub_buf<=rend_buf;last_yaw<=yaw_cur;
      last_render_cycles<=cyc;render_count<=render_count+1;r_state<=R_PUB;
     end else begin
      r_state<=R_RD;
      if(xasc?h==0:h==1)h<=~h;
      else begin
       h<=xasc?1'b0:1'b1;
       if(yasc?iy!=63:iy!=0)iy<=yasc?iy+1'b1:iy-1'b1;
       else begin iy<=yasc?6'd0:6'd63;iz<=zasc?iz+1'b1:iz-1'b1;end
      end
     end
    end
    R_PUB:begin
     // Two-phase: send until received, release, then wait for the video-side swap.
     if(!r_pub_wait)begin if(hs_rcv)begin hs_send<=0;r_pub_wait<=1;end end
     else if(!hs_rcv&&!hs_send)begin
      if(swap_sync!=swap_seen)begin
       swap_seen<=swap_sync;swap_count<=swap_count+1;rend_buf<=~rend_buf;
       if(auto_rotate)yaw_cur<=yaw_cur+yaw_step;
       r_state<=R_START;
      end else if(freeze)begin r_pend_swap<=1;r_state<=R_FROZEN;end
      else if(pub_to==PUB_RETRY)begin hs_send<=1;r_pub_wait<=0;pub_to<=0;republish_count<=republish_count+1;end
      else pub_to<=pub_to+1'b1;
     end
    end
    R_FROZEN:begin
     if(rb_req&&rb_addr[31])begin
      ca_addr<=rb_addr[16:0];
      if(rb_wait==3)begin rb_data<={28'b0,ca_q};rb_valid<=1;rb_req<=0;rb_wait<=0;end
      else rb_wait<=rb_wait+1'b1;
     end
     if(!freeze&&!(rb_req&&rb_addr[31]))begin rb_wait<=0;r_state<=r_pend_swap?R_PUB:R_START;end
    end
    default:r_state<=R_START;
   endcase
  end
 end

 // BRAM enables: on only for the cycles whose read data is consumed (or a write lands).
 //  stage: read issued in D_S1, used in D_S2.
 //  voxel A: address from R_RD, latched in R_RW1/R_RW2, used in R_WORD.
 //  voxel B: every drain/clear/readback state, plus the cycle a pending write lands.
 wire va_en=r_state==R_RW1||r_state==R_RW2;
 wire vb_en=vb_we||d_state!=D_IDLE;
 r40_stage_ram stage(.clk(aclk),.we(push),.waddr(wr_ptr[10:0]),.wdata(q5vaddr),
  .re(d_state==D_S1),.raddr(stage_raddr),.q(stage_q));
 r40_voxel_ram voxels(.clk(aclk),.en_a(va_en),.addr_a(va_addr),.q_a(va_q),
  .en_b(vb_en),.we_b(vb_we),.addr_b(vb_addr),.d_b(vb_d),.q_b(vb_q));

 xpm_cdc_handshake #(.DEST_EXT_HSK(0),.DEST_SYNC_FF(3),.INIT_SYNC_FF(0),.SIM_ASSERT_CHK(0),
  .SRC_SYNC_FF(3),.WIDTH(24)) publish(
  .src_clk(aclk),.src_in(hs_data),.src_send(hs_send),.src_rcv(hs_rcv),
  .dest_clk(vclk),.dest_out(hs_dest),.dest_req(hs_req),.dest_ack(1'b0));
 xpm_cdc_single #(.DEST_SYNC_FF(3),.INIT_SYNC_FF(0),.SIM_ASSERT_CHK(0),.SRC_INPUT_REG(1)) swap_back(
  .src_clk(vclk),.src_in(swap_tgl),.dest_clk(aclk),.dest_out(swap_sync));

 // ---------------------------------------------------------------- video overlay (vclk)
 wire adv=!m_video_tvalid||m_video_tready;
 assign s_video_tready=adv;
 wire s_fire=s_video_tvalid&&adv;
 reg req_d,pend;reg [23:0] pend_cfg,act_cfg;
 reg [10:0] xc,yc;
 wire [23:0] cfg=(s_video_tuser&&pend)?pend_cfg:act_cfg;
 wire c_en=cfg[23];wire [10:0] c_wx=cfg[22:12],c_wy=cfg[11:1];wire c_buf=cfg[0];
 wire [10:0] pos_x=s_video_tuser?11'd0:xc,pos_y=s_video_tuser?11'd0:yc;
 wire [11:0] rel_x=pos_x-c_wx,rel_y=pos_y-c_wy;
 wire inwin=c_en&&pos_x>=c_wx&&{1'b0,pos_x}<{1'b0,c_wx}+12'd448&&pos_y>=c_wy&&{1'b0,pos_y}<{1'b0,c_wy}+12'd480;
 reg st1_v,st1_l,st1_u,st1_w,st1_b;reg [31:0] st1_d;reg [7:0] st1_rx,st1_ry;
 reg st2_v,st2_l,st2_u,st2_w;reg [31:0] st2_d;reg [16:0] cb_addr;
 reg st3_v,st3_l,st3_u,st3_w;reg [31:0] st3_d;
 reg st4_v,st4_l,st4_u,st4_w;reg [31:0] st4_d;
 wire [3:0] cb_q;wire [23:0] pal;
 r40_palette palette(.i(cb_q),.rgb(pal));
 always @(posedge vclk)begin
  if(!vresetn)begin
   req_d<=0;pend<=0;pend_cfg<=0;act_cfg<=0;xc<=0;yc<=0;swap_tgl<=0;
   st1_v<=0;st2_v<=0;st3_v<=0;st4_v<=0;m_video_tvalid<=0;m_video_tdata<=0;m_video_tlast<=0;m_video_tuser<=0;
  end else begin
   req_d<=hs_req;
   if(hs_req&&!req_d)begin pend_cfg<=hs_dest;pend<=1;end
   if(s_fire)begin
    if(s_video_tuser&&pend)begin act_cfg<=pend_cfg;pend<=0;swap_tgl<=~swap_tgl;end
    if(s_video_tlast)begin xc<=0;yc<=pos_y+1'b1;end else begin xc<=pos_x+1'b1;yc<=pos_y;end
   end
   if(adv)begin
    st1_v<=s_fire;st1_d<=s_video_tdata;st1_l<=s_video_tlast;st1_u<=s_video_tuser;st1_w<=inwin;
    st1_rx<=rel_x[8:1];st1_ry<=rel_y[8:1];st1_b<=c_buf;
    st2_v<=st1_v;st2_d<=st1_d;st2_l<=st1_l;st2_u<=st1_u;st2_w<=st1_w;
    cb_addr<=(st1_b?17'd53760:17'd0)+{st1_ry,8'b0}-{st1_ry,5'b0}+st1_rx;
    st3_v<=st2_v;st3_d<=st2_d;st3_l<=st2_l;st3_u<=st2_u;st3_w<=st2_w;
    st4_v<=st3_v;st4_d<=st3_d;st4_l<=st3_l;st4_u<=st3_u;st4_w<=st3_w;
    m_video_tvalid<=st4_v;m_video_tlast<=st4_l;m_video_tuser<=st4_u;
    m_video_tdata<=(st4_w&&cb_q!=0)?{st4_d[31:24],pal}:st4_d;
   end
  end
 end
 // Colour A: writes, plus panel readback (only in R_FROZEN).
 // Colour B: b1 holds the st3 pixel and q_b the st4 pixel, so only advance while one
 // of them lies in the window; outside it (or with the overlay off) the port idles.
 r40_colour_ram colour(.clk_a(aclk),.en_a(ca_we||r_state==R_FROZEN),.we_a(ca_we),.addr_a(ca_addr),.d_a(ca_d),.q_a(ca_q),
  .clk_b(vclk),.en_b(adv&&(st2_w||st3_w)),.addr_b(cb_addr),.q_b(cb_q));
endmodule
