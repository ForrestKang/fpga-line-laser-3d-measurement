`timescale 1ns/1ps
// Descriptor-level equivalence stress. This bypasses pixel extraction equally
// in both engines and intentionally exercises four simultaneous matching lanes,
// including descriptors that cannot necessarily arise from a real pixel row.
module tb_pfo3_graph4;
 localparam H=8,N=H*16;
 reg clk=0;always #5 clk=~clk;
 reg rst=1,start=0,abort=0,load=0;
 reg [7:0] pixel_data=0;
 reg pixel_valid=0,pixel_sof=0,pixel_eof=0;
 reg record_ready=1;
 wire [511:0] rd4,rd1;
 wire [1:0] rk4,rk1;
 wire rv4,rv1,done4,done1;
 wire [31:0] mc4,mc1,pc4,pc1,ac4,ac1;
 wire [63:0] cycles4,cycles1;
 reg [95:0] seed_desc[0:N-1];
 reg [191:0] seed_rows[0:H-1];
 reg [513:0] got4[0:N+H-1],got1[0:N+H-1];
 integer n4=0,n1=0,checks=0,t=0,j,y,s,trial;
 reg ended4=0,ended1=0;
 reg [31:0] rng=32'h4bb47f1a;
 integer center,width;
 pfo3_frontend_r25 #(.FRAME_WIDTH(752),.FRAME_HEIGHT(H)) dut(
  .clk(clk),.rst(rst),.start(start),.abort(abort),.pixel_data(pixel_data),
  .pixel_valid(pixel_valid),.pixel_sof(pixel_sof),.pixel_eof(pixel_eof),
  .record_ready(record_ready),.record_data(rd4),.record_kind(rk4),.record_valid(rv4),
  .done(done4),.processing_cycles(cycles4),.measurement_count(mc4),.preview_count(pc4),.ambiguous_count(ac4));
 pfo3_frontend_serial_baseline #(.FRAME_WIDTH(752),.FRAME_HEIGHT(H)) gold(
  .clk(clk),.rst(rst),.start(start),.abort(abort),.pixel_data(pixel_data),
  .pixel_valid(pixel_valid),.pixel_sof(pixel_sof),.pixel_eof(pixel_eof),
  .record_ready(record_ready),.record_data(rd1),.record_kind(rk1),.record_valid(rv1),
  .done(done1),.processing_cycles(cycles1),.measurement_count(mc1),.preview_count(pc1),.ambiguous_count(ac1));
 genvar bank_i;
 generate for(bank_i=0;bank_i<4;bank_i=bank_i+1)begin:seed_bank
  integer a;
  always @(posedge clk)if(load)begin
   for(a=0;a<N/4;a=a+1)dut.desc_ram.bank[bank_i].ram.mem[a]<=seed_desc[a*4+bank_i];
  end
 end endgenerate
 always @(posedge clk)begin
  t=t+1;if(t>700000)$fatal(1,"graph equivalence watchdog");
  if(load)begin
   for(j=0;j<N;j=j+1)gold.descriptors[j]<=seed_desc[j];
   for(j=0;j<H;j=j+1)begin dut.rows[j]<=seed_rows[j];gold.rows[j]<=seed_rows[j];end
  end
  if(!rst)begin
   if(rv4&&record_ready)begin got4[n4]={rk4,rd4};n4=n4+1;end
   if(rv1&&record_ready)begin got1[n1]={rk1,rd1};n1=n1+1;end
   if(done4)ended4=1;
   if(done1)ended1=1;
  end
 end
 initial begin
  for(trial=0;trial<24;trial=trial+1)begin
   rst=1;start=0;load=0;ended4=0;ended1=0;n4=0;n1=0;
   for(y=0;y<H;y=y+1)begin
    seed_rows[y]=0;
    seed_rows[y][4:0]=16;
    // Every sixth trial contains a capacity-invalid row, excluded from edges.
    seed_rows[y][8]=(trial%6==5&&y==3);
    seed_rows[y][63:32]=16;seed_rows[y][95:64]=16;
    for(s=0;s<16;s=s+1)begin
     rng={rng[30:0],rng[31]^rng[21]^rng[1]^rng[0]};
     // First quartet dense: all four lane matches and degree reduction.
     // Remaining nodes: unique chains, broken edges, width rejection, jitter.
     center=(s<4)?100+s:160+s*20;
     center=center+((trial==0)?0:((rng>>7)%9)-4);
     width=3+((trial==0)?0:((rng>>12)%15));
     seed_desc[y*16+s]=0;
     seed_desc[y*16+s][17:0]=y*16+s;
     seed_desc[y*16+s][39:18]=center*4096+(rng&4095);
     seed_desc[y*16+s][49:40]=width;
     seed_desc[y*16+s][69:50]=(trial%3==1&&s==9)?16*4096:80*4096;
     seed_desc[y*16+s][77:70]=180;
    end
   end
   repeat(3)@(negedge clk);rst=0;start=1;
   @(negedge clk);start=0;
   repeat(3)@(negedge clk);load=1;
   @(negedge clk);load=0;
   dut.state=2;dut.node=0;gold.state=2;gold.node=0;
   wait(ended4&&ended1);@(negedge clk);
   if(n4!=n1||n4!=N+H)$fatal(1,"record counts %0d %0d",n4,n1);
   for(j=0;j<n4;j=j+1)begin
    if(got4[j]!==got1[j])$fatal(1,"trial %0d record %0d four=%h serial=%h",trial,j,got4[j],got1[j]);
    checks=checks+16;
   end
   if(mc4!==mc1||pc4!==pc1||ac4!==ac1)$fatal(1,"summary mismatch");
   if(cycles4>=cycles1)$fatal(1,"four-lane did not reduce cycles");
   $display("GRAPH_TRIAL_PASS trial=%0d fields=%0d four_cycles=%0d serial_cycles=%0d",trial,checks,cycles4,cycles1);
  end
  $display("TEST_PASS GRAPH4_SERIAL_EQUIVALENCE trials=24 fields=%0d",checks);$finish;
 end
endmodule

