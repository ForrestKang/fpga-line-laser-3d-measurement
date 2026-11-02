`timescale 1ns/1ps
module tb_pfo3_r25;
 parameter W=752,H=480;
 reg clk=0;always #5 clk=~clk;
 reg rst=1,start=0,abort=0;
 reg [7:0] pixel_data=0;
 reg pixel_valid=0,pixel_sof=0,pixel_eof=0;
 wire pixel_ready;
 wire [511:0] record_data;
 wire [1:0] record_kind;
 wire record_valid;
 reg record_ready=0;
 wire busy,done,error;
 wire [31:0] error_code,diagnostic_count,qualified_count,measurement_count,preview_count;
 wire [31:0] measurement_rows,preview_rows,ambiguous_count,sat250_count,sat255_count,flags;
 wire [31:0] measure_status,preview_status,input_bytes;
 wire [31:0] measure_median_peak_x2,measure_median_width_x2,preview_median_peak_x2,preview_median_width_x2,raw_peak_median_x2;
 wire [63:0] processing_cycles;
 wire frame_measurement_usable,frame_preview_usable;
 wire [31:0] frame_quality_flags,ambiguous_rows;
 pfo3_frontend_r25 #(.FRAME_WIDTH(W),.FRAME_HEIGHT(H)) dut(.*);
 reg [7:0] image[0:W*H-1];
 integer pos=0,cycles=0,fd,count_diag=0,count_patch=0,count_row=0,mode=0;
 integer pair_cycles=0,input_blocked_cycles=0,output_blocked_cycles=0,phase_fd;
 reg [1023:0] phase_path;
 reg stalled=0;reg [513:0] held;
 reg [31:0] rng=32'h75314d89;
 reg [1023:0] input_path,output_path,summary_path;
 initial begin
  if(!$value$plusargs("INPUT=%s",input_path))$fatal(1,"INPUT missing");
  if(!$value$plusargs("OUTPUT=%s",output_path))$fatal(1,"OUTPUT missing");
  if(!$value$plusargs("SUMMARY=%s",summary_path))$fatal(1,"SUMMARY missing");
  if($value$plusargs("MODE=%d",mode))begin end
  $readmemh(input_path,image);fd=$fopen(output_path,"w");
  repeat(5)@(negedge clk);rst=0;start=1;@(negedge clk);start=0;
 end
 always @(negedge clk)if(!rst)begin
  rng={rng[30:0],rng[31]^rng[21]^rng[1]^rng[0]};
  record_ready=rng[3:0]!=0;
  if(!pixel_valid||pixel_ready)begin
   pixel_valid=pos<W*H && rng[6:4]!=0;
   pixel_data=image[pos<W*H?pos:0];pixel_sof=pos==0;
   pixel_eof=pos==W*H-1;
   if(mode==1&&pos==11)pixel_sof=1;
  end
  if(mode==2&&cycles==200)abort=1;
 end
 always @(posedge clk)if(!rst)begin
  cycles=cycles+1;
  if(busy)begin
   if(dut.state==3||dut.state==4||dut.state==5||dut.state==22||dut.state==24||dut.state==25||dut.state==26)pair_cycles=pair_cycles+1;
   if(pixel_valid&&!pixel_ready&&dut.state==1)input_blocked_cycles=input_blocked_cycles+1;
   if(record_valid&&!record_ready)output_blocked_cycles=output_blocked_cycles+1;
  end
  if(pixel_valid&&pixel_ready)pos=pos+1;
  if(stalled&&!abort&&{record_kind,record_data}!==held)$fatal(1,"unstable stalled output");
  stalled=record_valid&&!record_ready;held={record_kind,record_data};
  if(record_valid&&record_ready)begin
   $fdisplay(fd,"%d %0128h",record_kind,record_data);
   if(record_kind==0)count_diag=count_diag+1;
   if(record_kind==1)count_patch=count_patch+1;
   if(record_kind==2)count_row=count_row+1;
  end
  if(done)begin
   $fclose(fd);fd=$fopen(summary_path,"w");
   $fdisplay(fd,"%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d",
    error,error_code,diagnostic_count,qualified_count,measurement_count,preview_count,
    measurement_rows,preview_rows,ambiguous_count,sat250_count,sat255_count,
    measure_status,preview_status,measure_median_peak_x2,measure_median_width_x2,
    preview_median_peak_x2,preview_median_width_x2,raw_peak_median_x2,processing_cycles,input_bytes,flags,
    frame_measurement_usable,frame_preview_usable,frame_quality_flags,ambiguous_rows);
   $fclose(fd);
   if($value$plusargs("PHASES=%s",phase_path))begin
    phase_fd=$fopen(phase_path,"w");
    $fdisplay(phase_fd,"%0d %0d %0d",pair_cycles,input_blocked_cycles,output_blocked_cycles);
    $fclose(phase_fd);
   end
   if(mode==0&&(error||count_diag!=diagnostic_count||count_row!=H))$fatal(1,"bad completion");
   if(mode==1&&(!error||error_code!=1))$fatal(1,"protocol fault not caught");
   if(mode==2&&(!error||error_code!=2))$fatal(1,"abort not caught");
   $display("TEST_PASS mode=%0d cycles=%0d diags=%0d patches=%0d rows=%0d",mode,processing_cycles,count_diag,count_patch,count_row);
   $finish;
  end
  if(cycles>20000000)$fatal(1,"watchdog state=%0d bytes=%0d",dut.state,input_bytes);
 end
endmodule


