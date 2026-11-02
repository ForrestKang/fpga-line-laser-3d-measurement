`timescale 1ns/1ps
module tb_pfo3_faults;
 reg clk=0;always #5 clk=~clk;
 reg rst=1,start=0,abort=0,feeding=0;
 reg [7:0] pixel_data=20;
 reg pixel_valid=0,pixel_sof=0,pixel_eof=0,record_ready=0;
 wire pixel_ready,record_valid,busy,done,error;
 wire [511:0] record_data;wire [1:0] record_kind;
 wire [31:0] error_code,measurement_count,preview_count;
 wire frame_measurement_usable,frame_preview_usable;
 pfo3_frontend_r25 #(.FRAME_WIDTH(64),.FRAME_HEIGHT(8)) dut(
  .clk(clk),.rst(rst),.start(start),.abort(abort),.pixel_data(pixel_data),
  .pixel_valid(pixel_valid),.pixel_sof(pixel_sof),.pixel_eof(pixel_eof),.pixel_ready(pixel_ready),
  .record_data(record_data),.record_kind(record_kind),.record_valid(record_valid),.record_ready(record_ready),
  .busy(busy),.done(done),.error(error),.error_code(error_code),.measurement_count(measurement_count),
  .preview_count(preview_count),.frame_measurement_usable(frame_measurement_usable),.frame_preview_usable(frame_preview_usable));
 integer graph_stage;
 integer pos=0,mode=0,dc=0,pc=0,rc=0,cycles=0;
 reg held_valid=0;reg [513:0] held;
 always @(negedge clk)begin
  if(!rst&&feeding&&(!pixel_valid||pixel_ready))begin
   pixel_valid=pos<512;pixel_data=(pos%64==32)?180:20;
   pixel_sof=pos==0;pixel_eof=(mode==1)?pos==17:((mode==2)?0:pos==511);
  end
 end
 always @(posedge clk)begin
  cycles=cycles+1;if(cycles>200000)$fatal(1,"watchdog");
  if(!rst)begin
   if(start||done)$display("phase t=%0t start=%0d done=%0d mode=%0d pos=%0d state=%0d busy=%0d",$time,start,done,mode,pos,dut.state,busy);
   if(pixel_valid&&pixel_ready)pos=pos+1;
   if(held_valid&&!abort&&({record_kind,record_data}!==held||!record_valid))$fatal(1,"stalled output changed");
   held_valid=record_valid&&!record_ready;held={record_kind,record_data};
   if(record_valid&&record_ready)case(record_kind)
    0:begin dc=dc+1;if(record_data[159:128]!==32*4096)$fatal(1,"center after restart");end
    1:begin pc=pc+1;if(record_data[447:416]!==32'h07000000||record_data[479:448]!==0)$fatal(1,"policy after restart");end
    2:rc=rc+1;
   endcase
  end else held_valid=0;
 end
 task launch(input integer fault);
 begin
  @(negedge clk);#1;mode=fault;pos=0;dc=0;pc=0;rc=0;
  pixel_valid=0;feeding=1;start=1;abort=0;
  @(negedge clk);#1;start=0;
 end endtask
 task expect_normal;
 begin
  wait(done);@(negedge clk);#1;feeding=0;pixel_valid=0;
  if(error||busy||dc!=8||pc!=8||rc!=8||measurement_count!=8||preview_count!=8||!frame_measurement_usable||!frame_preview_usable)
   $fatal(1,"restart failure dc=%0d pc=%0d rc=%0d",dc,pc,rc);
 end endtask
 task expect_protocol;
 begin
  wait(done);@(negedge clk);#1;feeding=0;pixel_valid=0;
  if(!error||error_code!=1||busy||frame_measurement_usable||frame_preview_usable)
   $fatal(1,"EOF protocol gate mode=%0d error=%0d code=%0d busy=%0d m=%0d p=%0d pos=%0d",mode,error,error_code,busy,frame_measurement_usable,frame_preview_usable,pos);
 end endtask
 initial begin
  repeat(5)@(negedge clk);#1;rst=0;
  launch(0);wait(record_valid&&record_kind==0);
  repeat(8)@(negedge clk);#1;abort=1;feeding=0;pixel_valid=0;
  wait(done);@(negedge clk);#1;
  if(!error||error_code!=2||busy||frame_measurement_usable||frame_preview_usable)$fatal(1,"STOP while record stalled");
  abort=0;record_ready=1;
  launch(0);expect_normal();
  launch(1);expect_protocol();
  launch(2);expect_protocol();
  launch(0);wait(pos>=17);@(negedge clk);#1;rst=1;feeding=0;pixel_valid=0;
  repeat(3)@(negedge clk);#1;rst=0;
  launch(0);expect_normal();
  for(graph_stage=24;graph_stage<=26;graph_stage=graph_stage+1)begin
   launch(0);wait(dut.state==graph_stage);@(negedge clk);#1;
   abort=1;feeding=0;pixel_valid=0;
   wait(done);@(negedge clk);#1;
   if(!error||error_code!=2||busy||frame_measurement_usable||frame_preview_usable)
    $fatal(1,"STOP in graph reduction state %0d",graph_stage);
   $display("GRAPH_STOP_PASS state=%0d",graph_stage);
   abort=0;launch(0);expect_normal();
  end
  $display("TEST_PASS STOP_STALLED_RECORD RESTART EARLY_EOF MISSING_EOF RESET_RESTART GRAPH_REDUCTION_STOP_RESTART");$finish;
 end
endmodule



