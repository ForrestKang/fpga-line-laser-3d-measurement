`timescale 1ns/1ps
// Transport wrapper: stable AXI-Lite control, AXIS32 unpack/serialize, no image math.
// Header snapshots all64 configurable provenance words. Native records are retained
// with kind tags; footer reports actual hardware counters. SG DMA owns full packet.
module pfo3_axi_r25 #(parameter FRAME_WIDTH=752,FRAME_HEIGHT=480)(
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 aclk CLK",X_INTERFACE_PARAMETER="FREQ_HZ 100000000, ASSOCIATED_BUSIF S_AXI:S_AXIS:M_AXIS, ASSOCIATED_RESET aresetn" *) input wire aclk,
 (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 aresetn RST",X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire aresetn,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *) input wire [11:0] s_axi_awaddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *) input wire s_axi_awvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *) output wire s_axi_awready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WDATA" *) input wire [31:0] s_axi_wdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *) input wire [3:0] s_axi_wstrb,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WVALID" *) input wire s_axi_wvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WREADY" *) output wire s_axi_wready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BRESP" *) output wire [1:0] s_axi_bresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BVALID" *) output reg s_axi_bvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BREADY" *) input wire s_axi_bready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *) input wire [11:0] s_axi_araddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *) input wire s_axi_arvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *) output wire s_axi_arready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RDATA" *) output reg [31:0] s_axi_rdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RRESP" *) output wire [1:0] s_axi_rresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RVALID" *) output reg s_axi_rvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RREADY" *) input wire s_axi_rready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 S_AXIS TDATA" *) input wire [31:0] s_axis_tdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 S_AXIS TKEEP" *) input wire [3:0] s_axis_tkeep,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 S_AXIS TVALID" *) input wire s_axis_tvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 S_AXIS TREADY" *) output wire s_axis_tready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 S_AXIS TLAST" *) input wire s_axis_tlast,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 M_AXIS TDATA" *) output reg [31:0] m_axis_tdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 M_AXIS TKEEP" *) output wire [3:0] m_axis_tkeep,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 M_AXIS TVALID" *) output wire m_axis_tvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 M_AXIS TREADY" *) input wire m_axis_tready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 M_AXIS TLAST" *) output wire m_axis_tlast,
 // R40: HDMI VDMA stream passes through the PL cloud view (video clock domain).
 input wire vclk,input wire vresetn,
 input wire [31:0] s_axis_video_tdata,input wire s_axis_video_tvalid,output wire s_axis_video_tready,
 input wire s_axis_video_tlast,input wire s_axis_video_tuser,
 output wire [31:0] m_axis_video_tdata,output wire m_axis_video_tvalid,input wire m_axis_video_tready,
 output wire m_axis_video_tlast,output wire m_axis_video_tuser,
 input wire [3:0] btn,
 // Live mode: camera raw stream from the capture core, and its pass-through to the raw DMA.
 input wire [31:0] s_axis_cam_tdata,input wire [3:0] s_axis_cam_tkeep,input wire s_axis_cam_tvalid,
 output wire s_axis_cam_tready,input wire s_axis_cam_tlast,
 output wire [31:0] m_axis_raw_tdata,output wire [3:0] m_axis_raw_tkeep,output wire m_axis_raw_tvalid,
 input wire m_axis_raw_tready,output wire m_axis_raw_tlast,
 output reg live_capture_request,input wire cam_ready
);
 localparam IDLE=0,HEAD=1,WAIT_REC=2,REC=3,FOOT=4;
 reg [2:0] state;
 reg [6:0] index;
 reg [31:0] config_words[0:63],snapshot[0:63];
 reg [31:0] footer_snapshot[0:31];
 reg [11:0] awaddr;
 reg awheld,wheld;
 (* max_fanout = 32 *) reg [31:0] wdata;
 reg [3:0] wstrb;
 reg start_pulse,abort_pulse,packet_busy,packet_done,done_seen;
 reg [31:0] words_out,sequence_number,transport_error;
 reg core_start,pending_start;
 reg [255:0] geo_cfg_sha,geo_active_sha;
 reg [31:0] geo_active_epoch;
 reg geo_admitted,geo_commit_error;
 reg [1:0] geo_commit_wait;
 reg metadata_pending;
 reg geo_cfg_we,geo_cfg_commit;
 reg [6:0] geo_cfg_addr;
 reg [63:0] geo_cfg_data;
 reg [31:0] geo_epoch;
 reg signed [31:0] moment_origin;
 reg [5:0] moment_shift;
 reg moments_finish,moments_requested,moments_complete;
 wire geometry_valid,geometry_ready,geo_out_valid,geo_out_ready,geo_busy,geo_cfg_ready,geo_cfg_error;
 wire [31:0] geometry_u,geometry_v,geometry_id,geo_out_epoch,geo_point_cycles;
 wire [1:0] geometry_modes;
 wire [255:0] geo_out_record,cache_data;
 wire cache_in_ready,cache_valid,cache_ready;
 wire [11:0] cache_occupancy,cache_high_water;
 wire moment_ready,moment_busy,moment_done,moment_overflow,moment_in_ready,moment_valid,moment_out_ready;
 wire [511:0] moment_data;
 wire [6:0] moment_bin;
 wire moment_half;
 wire [31:0] moment_accepted,moment_rejected;
 reg [31:0] geo_record_count;
 reg view_we;reg [7:0] view_waddr;reg [31:0] view_wdata;wire [31:0] view_rdata;
 // R41 live measurement statistics (rtl/r41_measure.sv), registers 0x500-0x5ff.
 reg meas_we;wire [31:0] meas_rdata;
 reg sec_we;wire [31:0] sec_rdata;
 wire [31:0] buttons_rdata;
 wire buttons_we=awheld && wheld && !s_axi_bvalid && awaddr[11:8]==6;
 wire core_busy,core_done,core_error,pixel_ready;
 wire [31:0] error_code,diagnostic_count,qualified_count,measurement_count,preview_count;
 wire [31:0] measurement_rows,preview_rows,ambiguous_count,sat250_count,sat255_count,flags;
 wire [31:0] measure_status,preview_status,measure_median_peak_x2,measure_median_width_x2;
 wire [31:0] preview_median_peak_x2,preview_median_width_x2,raw_peak_median_x2,input_bytes;
 wire frame_measurement_usable,frame_preview_usable;
 wire [31:0] frame_quality_flags,ambiguous_rows;
 wire [63:0] processing_cycles;
 wire [511:0] record_data;
 wire [1:0] record_kind;
 wire record_valid;
 wire record_ready=state==WAIT_REC && !done_seen;
 reg [511:0] record_hold;
 reg [7:0] kind_hold;
 reg [31:0] input_word;
 // live: 0x024 ctrl (b0 enable, b1 dump next packet, b2 copy next frame to the raw DMA),
 // 0x028 live packets, 0x02C live packets with an error footer, 0x030 last dump seq, 0x034 last copy seq
 reg live_enable,pkt_live,dump_pending,dump_frame,snap_pending,snap_frame;
 reg [31:0] live_frames,live_errors,last_dump_seq,last_snap_seq;
 reg [3:0] req_cool;
 reg input_valid,input_last;
 reg [1:0] byte_index;
 reg [31:0] byte_count;
 reg [31:0] received_words;
 wire pixel_valid=input_valid && packet_busy;
 wire pixel_fire=pixel_valid && pixel_ready;
 wire pfo3_ready=packet_busy && core_busy && received_words<FRAME_WIDTH*FRAME_HEIGHT/4 &&
                 (!input_valid || (pixel_fire && byte_index==3));
 wire snap_now=pkt_live && snap_frame;
 wire in_valid=pkt_live ? s_axis_cam_tvalid : s_axis_tvalid;
 wire in_ready=pfo3_ready && (!snap_now || m_axis_raw_tready);
 wire [31:0] in_data=pkt_live ? s_axis_cam_tdata : s_axis_tdata;
 wire [3:0] in_keep=pkt_live ? s_axis_cam_tkeep : s_axis_tkeep;
 wire in_last=pkt_live ? s_axis_cam_tlast : s_axis_tlast;
 assign s_axis_tready=!pkt_live && pfo3_ready;
 // replay: camera -> raw DMA unchanged; live: camera -> PFO3, plus the raw DMA for a copied frame
 assign s_axis_cam_tready=!live_enable && !pkt_live ? m_axis_raw_tready : pkt_live && in_ready;
 assign m_axis_raw_tvalid=!live_enable && !pkt_live ? s_axis_cam_tvalid : snap_now && s_axis_cam_tvalid && pfo3_ready;
 assign m_axis_raw_tdata=s_axis_cam_tdata;
 assign m_axis_raw_tkeep=s_axis_cam_tkeep;
 assign m_axis_raw_tlast=s_axis_cam_tlast;
 wire input_fire=in_valid && in_ready;
 assign s_axi_awready=!awheld && !s_axi_bvalid;
 assign s_axi_wready=!wheld && !s_axi_bvalid;
 assign s_axi_arready=!s_axi_rvalid;
 assign s_axi_bresp=0;assign s_axi_rresp=0;
 assign m_axis_tkeep=4'hf;
 wire out_valid=(state==HEAD || state==REC || state==FOOT);
 wire out_sink=pkt_live && !dump_frame;          // live packet nobody asked for: consumed here
 assign m_axis_tvalid=out_valid && !out_sink;
 wire out_fire=out_valid && (out_sink || m_axis_tready);
 assign m_axis_tlast=(state==FOOT && index==31);
 function automatic [31:0] summary(input [5:0] n);
 begin case(n)
 0:summary=32'h35544650;1:summary=35;2:summary=diagnostic_count;3:summary=qualified_count;
 4:summary=measurement_count;5:summary=preview_count;6:summary=measurement_rows;7:summary=preview_rows;
 8:summary=ambiguous_count;9:summary=sat250_count;10:summary=sat255_count;11:summary=flags;
 12:summary=measure_status;13:summary=preview_status;14:summary=processing_cycles[31:0];
 15:summary=processing_cycles[63:32];16:summary=input_bytes;17:summary=error_code|transport_error;
 18:summary=measure_median_peak_x2;19:summary=measure_median_width_x2;
 20:summary=preview_median_peak_x2;21:summary=preview_median_width_x2;
 22:summary=raw_peak_median_x2;23:summary=sequence_number;24:summary=words_out+32-index;
 25:summary=frame_measurement_usable;26:summary=frame_preview_usable;
 27:summary=frame_quality_flags;28:summary=ambiguous_rows;
 29:summary=geo_record_count;30:summary=moment_accepted;31:summary={31'b0,moment_overflow};
 default:summary=0;endcase end
 endfunction
 always @* begin
  m_axis_tdata=0;
  if(state==HEAD)begin
   case(index)
    0:m_axis_tdata=32'h35544850;1:m_axis_tdata=35;2:m_axis_tdata=FRAME_WIDTH;3:m_axis_tdata=FRAME_HEIGHT;
    4:m_axis_tdata=sequence_number;default:m_axis_tdata=snapshot[index];
   endcase
  end else if(state==REC)begin
   if(index==0)m_axis_tdata=32'h35520000|kind_hold;
   else m_axis_tdata=record_hold[(index-1)*32+:32];
  end else if(state==FOOT)m_axis_tdata=footer_snapshot[index[4:0]];
 end
 integer i,b;
 always @(posedge aclk)begin
  if(!aresetn)begin
   state<=IDLE;index<=0;awheld<=0;wheld<=0;s_axi_bvalid<=0;s_axi_rvalid<=0;s_axi_rdata<=0;
   core_start<=0;pending_start<=0;geo_cfg_sha<=0;geo_active_sha<=0;geo_active_epoch<=0;geo_admitted<=0;geo_commit_error<=0;geo_commit_wait<=0;metadata_pending<=0;geo_cfg_we<=0;geo_cfg_commit<=0;geo_cfg_addr<=0;geo_cfg_data<=0;geo_epoch<=0;
   moment_origin<=-32'sd65536;moment_shift<=16;moments_finish<=0;moments_requested<=0;moments_complete<=0;geo_record_count<=0;
   start_pulse<=0;abort_pulse<=0;packet_busy<=0;packet_done<=0;done_seen<=0;words_out<=0;
   sequence_number<=0;transport_error<=0;input_valid<=0;byte_index<=0;byte_count<=0;received_words<=0;
   view_we<=0;view_waddr<=0;view_wdata<=0;meas_we<=0;sec_we<=0;
   live_enable<=0;pkt_live<=0;dump_pending<=0;dump_frame<=0;snap_pending<=0;snap_frame<=0;
   live_frames<=0;live_errors<=0;last_dump_seq<=0;last_snap_seq<=0;req_cool<=0;live_capture_request<=0;
   for(i=0;i<64;i=i+1)begin config_words[i]<=0;snapshot[i]<=0;end
  end else begin
   start_pulse<=0;abort_pulse<=0;core_start<=0;geo_cfg_we<=0;geo_cfg_commit<=0;moments_finish<=0;view_we<=0;meas_we<=0;sec_we<=0;
   if(geo_commit_wait!=0)begin
    geo_commit_wait<=geo_commit_wait-1'b1;
    if(geo_cfg_error)geo_commit_error<=1;
    if(geo_commit_wait==1)begin
     if(!geo_commit_error&&!geo_cfg_error&&geo_cfg_ready)begin geo_admitted<=1;geo_active_epoch<=geo_epoch;geo_active_sha<=geo_cfg_sha;end
     else begin geo_admitted<=0;geo_commit_error<=1;end
    end
   end
   if(start_pulse)pending_start<=1;
   if(pending_start&&moment_ready&&geo_cfg_ready&&geo_admitted)begin core_start<=1;pending_start<=0;end
   if(done_seen&&!geo_busy&&!geo_out_valid&&!cache_valid&&cache_occupancy==0&&moment_ready&&!moments_requested)begin
    moments_finish<=1;moments_requested<=1;
   end
   if(moment_done)moments_complete<=1;
   if(geo_cfg_error)transport_error<=32'h400;
   if(geo_out_valid&&geo_out_ready)geo_record_count<=geo_record_count+1'b1;
   if(s_axi_bvalid && s_axi_bready)s_axi_bvalid<=0;
   if(s_axi_rvalid && s_axi_rready)s_axi_rvalid<=0;
   if(s_axi_awvalid && s_axi_awready)begin awaddr<=s_axi_awaddr;awheld<=1;end
   if(s_axi_wvalid && s_axi_wready)begin wdata<=s_axi_wdata;wstrb<=s_axi_wstrb;wheld<=1;end
   if(awheld && wheld && !s_axi_bvalid)begin
    awheld<=0;wheld<=0;s_axi_bvalid<=1;
    // R40 cloud view registers 0x400-0x4ff: full-word writes, any time.
    view_we<=awaddr[11:8]==4&&wstrb==4'hf;view_waddr<=awaddr[7:0];view_wdata<=wdata;
    meas_we<=awaddr[11:8]==5&&wstrb==4'hf;
    sec_we<=awaddr[11:8]==7&&wstrb==4'hf;
    if(awaddr==12'h024 && wstrb[0])begin
     live_enable<=wdata[0];
     if(wdata[1])dump_pending<=1;
     if(wdata[2])snap_pending<=1;
    end
    if(awaddr==12'h010 && wstrb[0])begin
     if(wdata[1])begin abort_pulse<=1;transport_error<=32'h100;input_valid<=0;end
     else if(wdata[0] && !live_enable && !packet_busy && !core_busy && geo_cfg_ready && geo_admitted && geo_commit_wait==0)begin
      start_pulse<=1;metadata_pending<=1;moments_requested<=0;moments_complete<=0;geo_record_count<=0;packet_busy<=1;packet_done<=0;done_seen<=0;state<=HEAD;index<=0;
      sequence_number<=sequence_number+1;words_out<=0;transport_error<=0;input_valid<=0;byte_count<=0;received_words<=0;
      for(i=0;i<64;i=i+1)snapshot[i]<=config_words[i];
      pkt_live<=0;dump_frame<=0;snap_frame<=0;
     end
    end
    if(!packet_busy&&geo_commit_wait==0&&wstrb==4'hf)begin
     if(awaddr>=12'h300&&awaddr<12'h360)geo_admitted<=0;
     if(awaddr>=12'h340&&awaddr<12'h360)geo_cfg_sha[awaddr[4:2]*32+:32]<=wdata;
     case(awaddr)
      12'h300:geo_cfg_addr<=wdata[6:0];
      12'h304:geo_cfg_data[31:0]<=wdata;
      12'h308:geo_cfg_data[63:32]<=wdata;
      12'h30c:begin geo_cfg_we<=wdata[0];geo_cfg_commit<=wdata[1];if(wdata[1])begin geo_commit_wait<=3;geo_commit_error<=0;end end
      12'h310:geo_epoch<=wdata;
      12'h314:moment_origin<=wdata;
      12'h318:moment_shift<=wdata[5:0];
      default:begin end
     endcase
    end
    if(awaddr[11:8]==1 && (!packet_busy || live_enable))begin
     for(b=0;b<4;b=b+1)if(wstrb[b])config_words[awaddr[7:2]][b*8+:8]<=wdata[b*8+:8];
    end
   end
   if(s_axi_arvalid && s_axi_arready)begin
    s_axi_rvalid<=1;
    case(s_axi_araddr)
     0:s_axi_rdata<=32'h57334650;4:s_axi_rdata<=32'h00035000;8:s_axi_rdata<=31;
     12'h014:s_axi_rdata<={29'd0,core_error,packet_done,packet_busy};
     12'h018:s_axi_rdata<=error_code|transport_error;
     12'h01c:s_axi_rdata<=sequence_number;12'h020:s_axi_rdata<=words_out*4;
     12'h024:s_axi_rdata<={27'd0,snap_frame,pkt_live,snap_pending,dump_pending,live_enable};
     12'h028:s_axi_rdata<=live_frames;12'h02c:s_axi_rdata<=live_errors;
     12'h030:s_axi_rdata<=last_dump_seq;12'h034:s_axi_rdata<=last_snap_seq;
     12'h30c:s_axi_rdata<={29'b0,(geo_commit_wait!=0),geo_commit_error,geo_admitted};
     12'h310:s_axi_rdata<=geo_epoch;
     12'h320:s_axi_rdata<=geo_active_epoch;
     12'h31c:s_axi_rdata<={20'b0,cache_high_water};
     default:begin
      if(s_axi_araddr[11:8]==1)s_axi_rdata<=config_words[s_axi_araddr[7:2]];
      else if(s_axi_araddr[11:8]==2)s_axi_rdata<=summary(s_axi_araddr[7:2]);
      else if(s_axi_araddr[11:8]==4)s_axi_rdata<=view_rdata;
      else if(s_axi_araddr[11:8]==5)s_axi_rdata<=meas_rdata;
      else if(s_axi_araddr[11:8]==6)s_axi_rdata<=buttons_rdata;
      else if(s_axi_araddr[11:8]==7)s_axi_rdata<=sec_rdata;
      else s_axi_rdata<=0;
     end
    endcase
   end
   if(pixel_fire)begin
    byte_count<=byte_count+1;
    if(byte_index==3)begin input_valid<=0;byte_index<=0;end
    else byte_index<=byte_index+1;
   end
   if(input_fire)begin
    received_words<=received_words+1;
    input_word<=in_data;input_valid<=1;input_last<=in_last;byte_index<=0;
    if(in_keep!=4'hf)begin abort_pulse<=1;transport_error<=32'h200;end
   end
   if(abort_pulse)begin geo_admitted<=0;geo_commit_wait<=0;geo_commit_error<=0;pending_start<=0;moments_complete<=1;moments_requested<=1;end
   if(core_done)done_seen<=1;
   // ---- live: start the next packet as soon as the previous one has left, then ask the
   // camera for frames until this packet has its whole input (a cut frame ends it with an error)
   live_capture_request<=0;
   if(req_cool!=0)req_cool<=req_cool-1'b1;
   if(live_enable&&!packet_busy&&!core_busy&&!pending_start&&!start_pulse&&!abort_pulse&&
      geo_cfg_ready&&geo_admitted&&geo_commit_wait==0)begin
    start_pulse<=1;metadata_pending<=1;moments_requested<=0;moments_complete<=0;geo_record_count<=0;packet_busy<=1;packet_done<=0;done_seen<=0;state<=HEAD;index<=0;
    sequence_number<=sequence_number+1;words_out<=0;transport_error<=0;input_valid<=0;byte_count<=0;received_words<=0;
    for(i=0;i<64;i=i+1)snapshot[i]<=config_words[i];
    pkt_live<=1;dump_frame<=dump_pending;dump_pending<=0;snap_frame<=snap_pending;snap_pending<=0;
   end else if(pkt_live&&packet_busy&&core_busy&&received_words<FRAME_WIDTH*FRAME_HEIGHT/4&&cam_ready&&req_cool==0)begin
    live_capture_request<=1;req_cool<=4'd15;
   end
   if(state==WAIT_REC)begin
    if(record_valid && record_ready)begin record_hold<=record_data;kind_hold<=record_kind;index<=0;state<=REC;end
    else if(cache_valid)begin record_hold<={256'b0,cache_data};kind_hold<=3;index<=0;state<=REC;end
    else if(metadata_pending)begin record_hold<={96'b0,32'd2,32'd1,26'b0,moment_shift,moment_origin,geo_active_sha,geo_active_epoch};kind_hold<=6;metadata_pending<=0;index<=0;state<=REC;end
    else if(moment_valid)begin record_hold<=moment_data;kind_hold<=moment_half?5:4;index<=0;state<=REC;end
    else if(done_seen&&moments_complete)begin
     state<=FOOT;index<=0;
     for(i=0;i<32;i=i+1)footer_snapshot[i]<=summary(i);
     footer_snapshot[24]<=words_out+32;
    end
   end
   if(out_fire)begin
    words_out<=words_out+1;
    if(state==HEAD)begin if(index==63)begin state<=WAIT_REC;index<=0;end else index<=index+1;end
    else if(state==REC)begin if(index==16)begin state<=WAIT_REC;index<=0;end else index<=index+1;end
    else if(state==FOOT)begin
     if(index==31)begin
      state<=IDLE;packet_busy<=0;packet_done<=1;index<=0;
      if(pkt_live)begin
       live_frames<=live_frames+1;
       if(footer_snapshot[17]!=0)live_errors<=live_errors+1;
       if(dump_frame)last_dump_seq<=sequence_number;
       if(snap_frame)last_snap_seq<=sequence_number;
      end
      pkt_live<=0;dump_frame<=0;snap_frame<=0;     // replay routing again between packets
     end else index<=index+1;
    end
   end
  end
 end
 pfo3_frontend_r25 #(.FRAME_WIDTH(FRAME_WIDTH),.FRAME_HEIGHT(FRAME_HEIGHT)) core(
 .clk(aclk),.rst(!aresetn),.start(core_start),.abort(abort_pulse),
 .bin_mode(snapshot[33]==32'd2),   // R41: header policy_revision 2 = 2x horizontal binning
 .seed_retry_mode(snapshot[33]==32'd3), // policy_revision 3: same-run seed retry (full width)
 .pixel_data(input_word[byte_index*8+:8]),.pixel_valid(pixel_valid),
 .pixel_sof(byte_count==0),.pixel_eof(input_last && byte_index==3),.pixel_ready(pixel_ready),
 .geometry_valid(geometry_valid),.geometry_ready(geometry_ready),
 .geometry_u_q12(geometry_u),.geometry_v_q12(geometry_v),.geometry_candidate_id(geometry_id),.geometry_modes(geometry_modes),
 .record_data(record_data),.record_kind(record_kind),.record_valid(record_valid),.record_ready(record_ready),
 .busy(core_busy),.done(core_done),.error(core_error),.error_code(error_code),
 .diagnostic_count(diagnostic_count),.qualified_count(qualified_count),
 .measurement_count(measurement_count),.preview_count(preview_count),
 .measurement_rows(measurement_rows),.preview_rows(preview_rows),.ambiguous_count(ambiguous_count),
 .sat250_count(sat250_count),.sat255_count(sat255_count),.flags(flags),
 .measure_status(measure_status),.preview_status(preview_status),
 .measure_median_peak_x2(measure_median_peak_x2),.measure_median_width_x2(measure_median_width_x2),
 .preview_median_peak_x2(preview_median_peak_x2),.preview_median_width_x2(preview_median_width_x2),
 .frame_measurement_usable(frame_measurement_usable),.frame_preview_usable(frame_preview_usable),
 .frame_quality_flags(frame_quality_flags),.ambiguous_rows(ambiguous_rows),
 .raw_peak_median_x2(raw_peak_median_x2),.processing_cycles(processing_cycles),.input_bytes(input_bytes));
 assign geo_out_ready=cache_in_ready&&moment_in_ready;
 assign cache_ready=state==WAIT_REC&&!record_valid;
 assign moment_out_ready=state==WAIT_REC&&!record_valid&&!cache_valid;
 r35_geometry geometry(.clk(aclk),.rst(!aresetn),.clear(abort_pulse),
  .cfg_we(geo_cfg_we),.cfg_addr(geo_cfg_addr),.cfg_wdata(geo_cfg_data),.cfg_commit(geo_cfg_commit),.cfg_epoch(geo_epoch),
  .cfg_ready(geo_cfg_ready),.cfg_error(geo_cfg_error),.in_valid(geometry_valid),.in_ready(geometry_ready),
  .u_q12(geometry_u),.v_q12(geometry_v),.candidate_id(geometry_id),.mode_bits(geometry_modes),
  .out_valid(geo_out_valid),.out_ready(geo_out_ready),.out_record(geo_out_record),.out_epoch(geo_out_epoch),
  .busy(geo_busy),.point_cycles(geo_point_cycles));
 r35_point_cache point_cache(.clk(aclk),.rst(!aresetn),.clear(start_pulse||abort_pulse),
  .in_data(geo_out_record),.in_valid(geo_out_valid&&moment_in_ready),.in_ready(cache_in_ready),
  .out_data(cache_data),.out_valid(cache_valid),.out_ready(cache_ready),.occupancy(cache_occupancy),.high_water(cache_high_water));
 // R40: read-only snoop of geometry records; a packet commits its points only when
 // its last footer beat leaves without error. Start/abort/error discard.
 wire foot_last=out_fire&&state==FOOT&&index==31;
 r42_buttons buttons(.clk(aclk),.resetn(aresetn),.btn(btn),
  .reg_we(buttons_we),.reg_waddr(awaddr[7:0]),.reg_wdata(wdata),.reg_wstrb(wstrb),
  .reg_raddr(s_axi_araddr[7:0]),.reg_rdata(buttons_rdata),.level(),.counts());
 r41_measure measure(.clk(aclk),.rst(!aresetn),
  .geo_fire(geo_out_valid&&geo_out_ready),.geo_record(geo_out_record),
  .pkt_commit(foot_last&&footer_snapshot[17]==0),
  .pkt_discard(start_pulse||abort_pulse||(foot_last&&footer_snapshot[17]!=0)),
  .reg_we(meas_we),.reg_waddr(view_waddr),.reg_wdata(view_wdata),
  .reg_raddr(s_axi_araddr[7:0]),.reg_rdata(meas_rdata));
 // Final section snoops the same accepted geometry and commit/discard boundary.
 // It never drives ready or changes the PFO3 stream. Disabled after reset.
 section_measure section(.clk(aclk),.rst(!aresetn),
  .geo_fire(geo_out_valid&&geo_out_ready),.geo_record(geo_out_record),
  .pkt_commit(foot_last&&footer_snapshot[17]==0),
  .pkt_discard(start_pulse||abort_pulse||(foot_last&&footer_snapshot[17]!=0)),
  .reg_we(sec_we),.reg_waddr(view_waddr),.reg_wdata(view_wdata),
  .reg_raddr(s_axi_araddr[7:0]),.reg_rdata(sec_rdata));
 r40_cloud_view view(.aclk(aclk),.aresetn(aresetn),
  .geo_fire(geo_out_valid&&geo_out_ready),.geo_record(geo_out_record),
  .pkt_commit(foot_last&&footer_snapshot[17]==0),
  .pkt_discard(start_pulse||abort_pulse||(foot_last&&footer_snapshot[17]!=0)),
  .reg_we(view_we),.reg_waddr(view_waddr),.reg_wdata(view_wdata),
  .reg_raddr(s_axi_araddr[7:0]),.reg_rdata(view_rdata),
  .vclk(vclk),.vresetn(vresetn),
  .s_video_tdata(s_axis_video_tdata),.s_video_tvalid(s_axis_video_tvalid),.s_video_tready(s_axis_video_tready),
  .s_video_tlast(s_axis_video_tlast),.s_video_tuser(s_axis_video_tuser),
  .m_video_tdata(m_axis_video_tdata),.m_video_tvalid(m_axis_video_tvalid),.m_video_tready(m_axis_video_tready),
  .m_video_tlast(m_axis_video_tlast),.m_video_tuser(m_axis_video_tuser));
 // R40 b08: r35_moments removed (its height-bin statistics had no consumer). The finish
 // handshake is kept so the footer still follows the last geometry record; no kind 4/5
 // records are emitted and footer words 30/31 read 0. 0x314/0x318 stay writable and are
 // still echoed in the kind 6 metadata record, so the ARM register ABI is unchanged.
 assign moment_ready=1'b1,moment_in_ready=1'b1,moment_busy=1'b0,moment_done=moments_finish;
 assign moment_overflow=1'b0,moment_valid=1'b0,moment_data=512'b0,moment_bin=7'b0,moment_half=1'b0;
 assign moment_accepted=32'b0,moment_rejected=32'b0;
endmodule
