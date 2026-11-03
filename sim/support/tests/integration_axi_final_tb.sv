`timescale 1ns/1ps
module integration_axi_tb;
 reg [4095:0] section_path;
 reg [31:0] section_config[0:14];
 integer sc, section_attempts;
 reg section_test=0;
 reg [3:0] btn=0;
 localparam W=752,H=480;
 reg aclk=0;always #5 aclk=~aclk;
 reg aresetn=0;
 reg [11:0] s_axi_awaddr=0,s_axi_araddr=0;
 reg s_axi_awvalid=0,s_axi_wvalid=0,s_axi_bready=1,s_axi_arvalid=0,s_axi_rready=1;
 reg [31:0] s_axi_wdata=0,s_axis_tdata=0;
 reg [3:0] s_axi_wstrb=15,s_axis_tkeep=15;
 reg s_axis_tvalid=0,s_axis_tlast=0;
 wire s_axi_awready,s_axi_wready,s_axi_bvalid,s_axi_arready,s_axi_rvalid,s_axis_tready;
 wire [1:0] s_axi_bresp,s_axi_rresp;
 wire [31:0] s_axi_rdata,m_axis_tdata;
 wire [3:0] m_axis_tkeep;
 wire m_axis_tvalid,m_axis_tlast;
 reg [31:0] rng=32'h35350101;
 wire m_axis_tready=!rng[0]||rng[3];
 reg [7:0] pixels[0:W*H-1];
 reg [63:0] params[0:43];
 reg [31:0] header_words[0:63];
 reg [4095:0] input_path,config_path,output_path,header_path;
 reg have_header=0;
 integer file_out,cycle=0,words=0,j;
 reg [31:0] read_value;
 reg packet_done=0,held=0;
 reg [31:0] held_word;reg held_last;
 // R40: video side idle here (the cloud view is covered by tb_r40_view); ingest is on.
 reg vclk=0;always #3.5 vclk=~vclk;
 reg vresetn=0;
 reg [31:0] s_axis_video_tdata=0;reg s_axis_video_tvalid=0,s_axis_video_tlast=0,s_axis_video_tuser=0;
 wire s_axis_video_tready;
 wire [31:0] m_axis_video_tdata;wire m_axis_video_tvalid,m_axis_video_tlast,m_axis_video_tuser;
 reg m_axis_video_tready=1;
 reg [31:0] v_ins,v_drop,v_com,v_dis,v_occ,v_oor;
 integer hk;
 // live mode: camera-core model on S_AXIS_CAM, raw DMA always ready
 reg [31:0] s_axis_cam_tdata=0;reg [3:0] s_axis_cam_tkeep=15;reg s_axis_cam_tvalid=0,s_axis_cam_tlast=0;
 wire s_axis_cam_tready;wire [31:0] m_axis_raw_tdata;wire [3:0] m_axis_raw_tkeep;
 wire m_axis_raw_tvalid,m_axis_raw_tlast;reg m_axis_raw_tready=1;
 wire live_capture_request;reg cam_ready=0;
 integer live_n=0,live_done=0,cam_frames=0,cw;
 axi_packet_controller dut(.*);
 // camera core: a request starts one frame after a short FV wait; words at about camera speed
 always @(posedge aclk)if(live_capture_request&&cam_ready)begin
  cam_ready<=0;
  fork begin
   repeat(200)@(negedge aclk);
   for(cw=0;cw<W*H/4;cw=cw+1)begin
    repeat(3)@(negedge aclk);
    s_axis_cam_tvalid=1;s_axis_cam_tlast=cw==W*H/4-1;
    s_axis_cam_tdata={pixels[4*cw+3],pixels[4*cw+2],pixels[4*cw+1],pixels[4*cw]};
    @(posedge aclk);while(!s_axis_cam_tready)@(posedge aclk);
    @(negedge aclk);s_axis_cam_tvalid=0;
   end
   s_axis_cam_tlast=0;cam_frames=cam_frames+1;
   repeat(20)@(negedge aclk);cam_ready=1;
  end join_none
 end
 // live tail: last camera word accepted -> footer end -> next capture request (cycles)
 integer t_cyc=0,t_last=0,t_foot=0;
 always @(posedge aclk)begin
  t_cyc<=t_cyc+1;
  if(s_axis_cam_tvalid&&s_axis_cam_tready&&s_axis_cam_tlast)t_last<=t_cyc;
  if(dut.foot_last)t_foot<=t_cyc;
  if(live_capture_request&&t_last>0)$display("LIVE_TAIL last_to_foot=%0d foot_to_req=%0d last_to_req=%0d",t_foot-t_last,t_cyc-t_foot,t_cyc-t_last);
 end
 task write_register(input [11:0] address,input [31:0] value);
 begin
  @(negedge aclk);s_axi_awaddr=address;s_axi_wdata=value;s_axi_awvalid=1;s_axi_wvalid=1;
  @(posedge aclk);while(!s_axi_awready||!s_axi_wready)@(posedge aclk);
  @(negedge aclk);s_axi_awvalid=0;s_axi_wvalid=0;
  while(!s_axi_bvalid)@(negedge aclk);
  if(s_axi_bresp!=0)$fatal(1,"AXI write error %h",address);
  @(negedge aclk);
 end
 endtask
 task read_register(input [11:0] address,output [31:0] value);
 begin
  @(negedge aclk);s_axi_araddr=address;s_axi_arvalid=1;
  @(posedge aclk);while(!s_axi_arready)@(posedge aclk);
  @(negedge aclk);s_axi_arvalid=0;
  while(!s_axi_rvalid)@(negedge aclk);
  if(s_axi_rresp!=0)$fatal(1,"AXI read error %h",address);
  value=s_axi_rdata;
  @(negedge aclk);
 end
 endtask
 task wait_admission(input bit accepted);
 integer attempts;reg [31:0] status;
 begin
  status=4;attempts=0;
  while(status[2]&&attempts<32)begin
   read_register(12'h30c,status);attempts=attempts+1;
  end
  if(status[2]||status[0]!==accepted||status[1]===accepted)
   $fatal(1,"geometry admission status %h expected accepted=%b",status,accepted);
 end
 endtask
 task expect_start_rejected;
 begin
  write_register(12'h010,1);
  repeat(16)@(negedge aclk);
  if(dut.packet_busy||dut.pending_start||dut.core_start||m_axis_tvalid||s_axis_tready)
   $fatal(1,"non-admitted geometry accepted START");
 end
 endtask
 initial begin
  if(!$value$plusargs("INPUT=%s",input_path))$fatal(1,"INPUT required");
  if(!$value$plusargs("CONFIG=%s",config_path))$fatal(1,"CONFIG required");
  if(!$value$plusargs("OUTPUT=%s",output_path))$fatal(1,"OUTPUT required");
  $readmemh(input_path,pixels);$readmemh(config_path,params);
  have_header=$value$plusargs("HEADER=%s",header_path);
  if(have_header)$readmemh(header_path,header_words);
  file_out=$fopen(output_path,"w");
  repeat(8)@(negedge aclk);aresetn=1;vresetn=1;
  write_register(12'h400,32'h0a);                      // R40 view: insert enable + clear
  read_value=1;while(read_value[0])read_register(12'h464,read_value);
  read_register(12'h468,read_value);
  if(read_value!=32'h52343056)$fatal(1,"R40 view magic %h",read_value);
  for(j=0;j<36;j=j+1)begin
   write_register(12'h300,j);write_register(12'h304,params[j][31:0]);
   write_register(12'h308,params[j][63:32]);write_register(12'h30c,1);
  end
  // Every geometry/SHA write invalidates admission. SHA must precede COMMIT.
  for(j=0;j<8;j=j+1)write_register(12'h340+j*4,params[36+j][31:0]);
  write_register(12'h310,35);write_register(12'h30c,2);wait_admission(1);
  read_register(12'h320,read_value);
  if(read_value!=35)$fatal(1,"active epoch after valid commit %h",read_value);
  // A failed shadow commit must not reuse the core's previous cfg_ready.
  write_register(12'h300,2);write_register(12'h304,0);write_register(12'h308,0);write_register(12'h30c,1);
  read_register(12'h30c,read_value);
  if(read_value[0])$fatal(1,"shadow write retained admission");
  expect_start_rejected();
  write_register(12'h310,36);write_register(12'h30c,2);wait_admission(0);
  read_register(12'h320,read_value);
  if(read_value!=35||!dut.geo_cfg_ready)$fatal(1,"invalid commit changed active configuration");
  expect_start_rejected();
  write_register(12'h300,2);write_register(12'h304,params[2][31:0]);
  write_register(12'h308,params[2][63:32]);write_register(12'h30c,1);
  write_register(12'h310,35);write_register(12'h30c,2);wait_admission(1);
  $display("ADMISSION_NEGATIVE_PASS shadow-write and invalid-after-valid START rejected");
  // Provenance snapshot is deliberately nonzero, to expose register/address
  // aliasing and accidental geometry/config overlap.
  for(j=5;j<64;j=j+1)write_register(12'h100+j*4,have_header?header_words[j]:32'h35000000+j);
  // R41 measurement statistics: preview-grade points, Z bins 4 mm from 0, height bands 2 mm from -64 mm.
  write_register(12'h508,0);write_register(12'h50c,18);write_register(12'h510,-32'sd4194304);
  write_register(12'h514,17);write_register(12'h504,3);
  section_test=$value$plusargs("SECTION_CONFIG=%s",section_path);
  if(section_test)begin
   $readmemh(section_path,section_config);
   read_register(12'h700,read_value);
   if(read_value!==32'h5345434d)$fatal(1,"section magic %h",read_value);
   for(sc=0;sc<15;sc=sc+1)write_register(12'h708+sc*4,section_config[sc]);
   write_register(12'h704,3);
  end
  if(!$value$plusargs("LIVE=%d",live_n))live_n=0;
  if(live_n>0)begin
   cam_ready=1;
   write_register(12'h024,3);                     // live on, dump the first packet
   while(!packet_done)@(negedge aclk);
   read_register(12'h028,read_value);
   while(read_value<live_n)read_register(12'h028,read_value);
   write_register(12'h024,0);
   while(dut.packet_busy)@(negedge aclk);
   read_register(12'h02c,read_value);
   if(read_value!=0)$fatal(1,"live packets with errors %0d",read_value);
   read_register(12'h030,read_value);
   if(read_value!=1)$fatal(1,"dumped sequence %0d",read_value);
   read_register(12'h028,read_value);live_done=read_value;
   $display("LIVE_PASS live_frames=%0d camera_frames=%0d",read_value,cam_frames);
  end else begin
  write_register(12'h010,1);
  for(j=0;j<W*H/4;j=j+1)begin
   @(negedge aclk);
   s_axis_tvalid=1;s_axis_tlast=j==W*H/4-1;
   s_axis_tdata={pixels[4*j+3],pixels[4*j+2],pixels[4*j+1],pixels[4*j]};
   @(posedge aclk);while(!s_axis_tready)@(posedge aclk);
   @(negedge aclk);s_axis_tvalid=0;
  end
  @(negedge aclk);s_axis_tlast=0;
  while(!packet_done)@(negedge aclk);
  end
  $fclose(file_out);
  repeat(64)@(negedge aclk);
  while(dut.view.rd_ptr!=dut.view.cm_ptr||dut.view.d_state!=0)@(negedge aclk);
  read_register(12'h440,v_ins);read_register(12'h444,v_drop);read_register(12'h448,v_com);
  read_register(12'h44c,v_dis);read_register(12'h450,v_occ);read_register(12'h454,v_oor);
  $display("VIEW_STATUS inserted=%0d dropped=%0d committed=%0d discarded=%0d occupied=%0d out_of_range=%0d",
   v_ins,v_drop,v_com,v_dis,v_occ,v_oor);
  repeat(300)@(negedge aclk);   // end-of-packet median scan (256 cycles)
  $write("MEASURE_R");
  for(hk=8'h20;hk<=8'h60;hk=hk+4)begin read_register(12'h500+hk,read_value);$write(" %0d",read_value);end
  read_register(12'h580,read_value);$display(" %0d",read_value);
  for(hk=0;hk<32;hk=hk+1)begin
   write_register(12'h518,hk);$write("MEASURE_B %0d",hk);
   for(j=8'h64;j<=8'h7c;j=j+4)begin read_register(12'h500+j,read_value);$write(" %0d",read_value);end
   $display("");
  end
  if(dut.transport_error!=0||dut.error_code!=0)$fatal(1,"hardware error %h/%h",dut.transport_error,dut.error_code);
  if(section_test)begin
   section_attempts=0;read_register(12'h750,read_value);
   while(read_value!=(live_n>0?live_done:1)&&section_attempts<2000000)begin
    read_register(12'h750,read_value);section_attempts=section_attempts+1;
   end
   if(read_value!=(live_n>0?live_done:1))$fatal(1,"section solve deadline");
   $write("SECTION_R");
   for(sc=8'h50;sc<=8'h7c;sc=sc+4)begin read_register(12'h700+sc,read_value);$write(" %08x",read_value);end
   read_register(12'h79c,read_value);$write(" %08x",read_value);
   read_register(12'h7a0,read_value);$write(" %08x",read_value);
   read_register(12'h7a4,read_value);$display(" %08x",read_value);
   for(sc=0;sc<16;sc=sc+1)begin
    write_register(12'h744,sc|(sc<<8));$write("SECTION_T %0d",sc);
    for(hk=8'h80;hk<=8'h98;hk=hk+4)begin read_register(12'h700+hk,read_value);$write(" %08x",read_value);end
    $display("");
   end
  end
  $display("TEST_PASS words=%0d cycles=%0d geo=%0d moments=%0d",words,cycle,dut.geo_record_count,dut.moment_accepted);
  $finish;
 end
 always @(posedge aclk)if(aresetn)begin
  cycle<=cycle+1;rng<={rng[30:0],rng[31]^rng[21]^rng[1]^rng[0]};
  if(held&&(!m_axis_tvalid||m_axis_tdata!==held_word||m_axis_tlast!==held_last))$fatal(1,"AXIS output changed under backpressure");
  held<=m_axis_tvalid&&!m_axis_tready;held_word<=m_axis_tdata;held_last<=m_axis_tlast;
  if(m_axis_tvalid&&m_axis_tready)begin
   if(m_axis_tkeep!=15)$fatal(1,"partial output word");
   $fdisplay(file_out,"%08x",m_axis_tdata);words<=words+1;
   if(m_axis_tlast)packet_done<=1;
  end
  if(cycle>40000000)$fatal(1,"integration timeout state=%0d core=%0d",dut.state,dut.core.state);
 end
endmodule
