`timescale 1ns/1ps
// Transport wrapper: stable AXI-Lite control, AXIS32 unpack/serialize, no image math.
// Header snapshots all64 configurable provenance words. Native records are retained
// with kind tags; footer reports actual hardware counters. SG DMA owns full packet.
module axi_packet_controller #(parameter FRAME_WIDTH=752,FRAME_HEIGHT=480)(
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
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 M_AXIS TLAST" *) output wire m_axis_tlast
);
 localparam IDLE=0,HEAD=1,WAIT_REC=2,REC=3,FOOT=4;
 reg [2:0] state;
 reg [6:0] index;
 reg [31:0] config_words[0:63],snapshot[0:63];
 reg [31:0] footer_snapshot[0:31];
 reg [11:0] awaddr;
 reg awheld,wheld;
 reg [31:0] wdata;
 reg [3:0] wstrb;
 reg start_pulse,abort_pulse,packet_busy,packet_done,done_seen;
 reg [31:0] words_out,sequence_number,transport_error;
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
 reg [1:0] kind_hold;
 reg [31:0] input_word;
 reg input_valid,input_last;
 reg [1:0] byte_index;
 reg [31:0] byte_count;
 reg [31:0] received_words;
 wire pixel_valid=input_valid && packet_busy;
 wire pixel_fire=pixel_valid && pixel_ready;
 assign s_axis_tready=packet_busy && core_busy && received_words<FRAME_WIDTH*FRAME_HEIGHT/4 &&
                     (!input_valid || (pixel_fire && byte_index==3));
 wire input_fire=s_axis_tvalid && s_axis_tready;
 assign s_axi_awready=!awheld && !s_axi_bvalid;
 assign s_axi_wready=!wheld && !s_axi_bvalid;
 assign s_axi_arready=!s_axi_rvalid;
 assign s_axi_bresp=0;assign s_axi_rresp=0;
 assign m_axis_tkeep=4'hf;
 assign m_axis_tvalid=(state==HEAD || state==REC || state==FOOT);
 assign m_axis_tlast=(state==FOOT && index==31);
 function automatic [31:0] summary(input [5:0] n);
 begin case(n)
 0:summary=32'h33544650;1:summary=1;2:summary=diagnostic_count;3:summary=qualified_count;
 4:summary=measurement_count;5:summary=preview_count;6:summary=measurement_rows;7:summary=preview_rows;
 8:summary=ambiguous_count;9:summary=sat250_count;10:summary=sat255_count;11:summary=flags;
 12:summary=measure_status;13:summary=preview_status;14:summary=processing_cycles[31:0];
 15:summary=processing_cycles[63:32];16:summary=input_bytes;17:summary=error_code|transport_error;
 18:summary=measure_median_peak_x2;19:summary=measure_median_width_x2;
 20:summary=preview_median_peak_x2;21:summary=preview_median_width_x2;
 22:summary=raw_peak_median_x2;23:summary=sequence_number;24:summary=words_out+32-index;
 25:summary=frame_measurement_usable;26:summary=frame_preview_usable;
 27:summary=frame_quality_flags;28:summary=ambiguous_rows;
 default:summary=0;endcase end
 endfunction
 always @* begin
  m_axis_tdata=0;
  if(state==HEAD)begin
   case(index)
    0:m_axis_tdata=32'h33544850;1:m_axis_tdata=1;2:m_axis_tdata=FRAME_WIDTH;3:m_axis_tdata=FRAME_HEIGHT;
    4:m_axis_tdata=sequence_number;default:m_axis_tdata=snapshot[index];
   endcase
  end else if(state==REC)begin
   if(index==0)m_axis_tdata=32'h33520000|kind_hold;
   else m_axis_tdata=record_hold[(index-1)*32+:32];
  end else if(state==FOOT)m_axis_tdata=footer_snapshot[index[4:0]];
 end
 integer i,b;
 always @(posedge aclk)begin
  if(!aresetn)begin
   state<=IDLE;index<=0;awheld<=0;wheld<=0;s_axi_bvalid<=0;s_axi_rvalid<=0;s_axi_rdata<=0;
   start_pulse<=0;abort_pulse<=0;packet_busy<=0;packet_done<=0;done_seen<=0;words_out<=0;
   sequence_number<=0;transport_error<=0;input_valid<=0;byte_index<=0;byte_count<=0;received_words<=0;
   for(i=0;i<64;i=i+1)begin config_words[i]<=0;snapshot[i]<=0;end
  end else begin
   start_pulse<=0;abort_pulse<=0;
   if(s_axi_bvalid && s_axi_bready)s_axi_bvalid<=0;
   if(s_axi_rvalid && s_axi_rready)s_axi_rvalid<=0;
   if(s_axi_awvalid && s_axi_awready)begin awaddr<=s_axi_awaddr;awheld<=1;end
   if(s_axi_wvalid && s_axi_wready)begin wdata<=s_axi_wdata;wstrb<=s_axi_wstrb;wheld<=1;end
   if(awheld && wheld && !s_axi_bvalid)begin
    awheld<=0;wheld<=0;s_axi_bvalid<=1;
    if(awaddr==12'h010 && wstrb[0])begin
     if(wdata[1])begin abort_pulse<=1;transport_error<=32'h100;input_valid<=0;end
     else if(wdata[0] && !packet_busy && !core_busy)begin
      start_pulse<=1;packet_busy<=1;packet_done<=0;done_seen<=0;state<=HEAD;index<=0;
      sequence_number<=sequence_number+1;words_out<=0;transport_error<=0;input_valid<=0;byte_count<=0;received_words<=0;
      for(i=0;i<64;i=i+1)snapshot[i]<=config_words[i];
     end
    end
    if(awaddr[11:8]==1 && !packet_busy)begin
     for(b=0;b<4;b=b+1)if(wstrb[b])config_words[awaddr[7:2]][b*8+:8]<=wdata[b*8+:8];
    end
   end
   if(s_axi_arvalid && s_axi_arready)begin
    s_axi_rvalid<=1;
    case(s_axi_araddr)
     0:s_axi_rdata<=32'h57334650;4:s_axi_rdata<=32'h00010000;8:s_axi_rdata<=7;
     12'h014:s_axi_rdata<={29'd0,core_error,packet_done,packet_busy};
     12'h018:s_axi_rdata<=error_code|transport_error;
     12'h01c:s_axi_rdata<=sequence_number;12'h020:s_axi_rdata<=words_out*4;
     default:begin
      if(s_axi_araddr[11:8]==1)s_axi_rdata<=config_words[s_axi_araddr[7:2]];
      else if(s_axi_araddr[11:8]==2)s_axi_rdata<=summary(s_axi_araddr[7:2]);
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
    input_word<=s_axis_tdata;input_valid<=1;input_last<=s_axis_tlast;byte_index<=0;
    if(s_axis_tkeep!=4'hf)begin abort_pulse<=1;transport_error<=32'h200;end
   end
   if(core_done)done_seen<=1;
   if(state==WAIT_REC)begin
    if(record_valid && record_ready)begin record_hold<=record_data;kind_hold<=record_kind;index<=0;state<=REC;end
    else if(done_seen)begin
     state<=FOOT;index<=0;
     for(i=0;i<32;i=i+1)footer_snapshot[i]<=summary(i);
     footer_snapshot[24]<=words_out+32;
    end
   end
   if(m_axis_tvalid && m_axis_tready)begin
    words_out<=words_out+1;
    if(state==HEAD)begin if(index==63)begin state<=WAIT_REC;index<=0;end else index<=index+1;end
    else if(state==REC)begin if(index==16)begin state<=WAIT_REC;index<=0;end else index<=index+1;end
    else if(state==FOOT)begin
     if(index==31)begin state<=IDLE;packet_busy<=0;packet_done<=1;index<=0;end else index<=index+1;
    end
   end
  end
 end
 pixel_frontend #(.FRAME_WIDTH(FRAME_WIDTH),.FRAME_HEIGHT(FRAME_HEIGHT)) core(
 .clk(aclk),.rst(!aresetn),.start(start_pulse),.abort(abort_pulse),
 .pixel_data(input_word[byte_index*8+:8]),.pixel_valid(pixel_valid),
 .pixel_sof(byte_count==0),.pixel_eof(input_last && byte_index==3),.pixel_ready(pixel_ready),
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
endmodule
