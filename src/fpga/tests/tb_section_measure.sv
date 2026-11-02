`timescale 1ns/1ps
// Drives section_measure from a script and dumps the results of every committed packet.
//   W <addr> <data>     register write (hex)
//   R <w0> .. <w7>      one geometry record (hex words), then +GAP idle cycles
//   C                   commit, wait for the solve, dump results
//   D                   discard (packet start / abort)
module tb_section_measure;
 reg clk = 0, rst = 1;
 always #5 clk = ~clk;
 reg geo_fire = 0, pkt_commit = 0, pkt_discard = 0, reg_we = 0;
 reg [255:0] geo_record = 0;
 reg [7:0] reg_waddr = 0, reg_raddr = 0;
 reg [31:0] reg_wdata = 0;
 wire [31:0] reg_rdata;
 section_measure dut(.clk(clk), .rst(rst), .geo_fire(geo_fire), .geo_record(geo_record),
  .pkt_commit(pkt_commit), .pkt_discard(pkt_discard), .reg_we(reg_we), .reg_waddr(reg_waddr),
  .reg_wdata(reg_wdata), .reg_raddr(reg_raddr), .reg_rdata(reg_rdata));

 integer fin, fout, gap, i, r, packets;
 reg [8*4-1:0] tok;
 reg [31:0] a, d, w [0:7], prev_cnt;

 task automatic rd(input [7:0] addr, output [31:0] v);
  begin reg_raddr = addr; #1; v = reg_rdata; end
 endtask
 task automatic wr(input [7:0] addr, input [31:0] v);
  begin
   @(negedge clk); reg_we = 1; reg_waddr = addr; reg_wdata = v;
   @(negedge clk); reg_we = 0;
  end
 endtask
 task automatic dump;
  reg [31:0] v;
  integer j;
  begin
   $fwrite(fout, "P");
   for (j = 8'h50; j <= 8'hA8; j = j + 4) begin
    if (j < 8'h80 || j >= 8'h9C) begin rd(j, v); $fwrite(fout, " %08x", v); end
   end
   $fwrite(fout, "\n");
   for (j = 0; j < 16; j = j + 1) begin
    wr(8'h44, (j << 8) | j);
    $fwrite(fout, "T %0d", j);
    rd(8'h80, v); $fwrite(fout, " %08x", v);
    rd(8'h84, v); $fwrite(fout, " %08x", v);
    rd(8'h88, v); $fwrite(fout, " %08x", v);
    rd(8'h8C, v); $fwrite(fout, " %08x", v);
    rd(8'h90, v); $fwrite(fout, " %08x", v);
    rd(8'h94, v); $fwrite(fout, " %08x", v);
    rd(8'h98, v); $fwrite(fout, " %08x\n", v);
   end
  end
 endtask

 initial begin
  if (!$value$plusargs("GAP=%d", gap)) gap = 3;
  fin = $fopen(`SCRIPT, "r");
  fout = $fopen(`RESULT, "w");
  if (fin == 0 || fout == 0) begin $display("cannot open files"); $finish; end
  repeat (4) @(negedge clk);
  rst = 0;
  packets = 0;
  while (!$feof(fin)) begin
   r = $fscanf(fin, "%s", tok);
   if (r != 1) ;
   else if (tok == "W") begin
    r = $fscanf(fin, "%h %h", a, d);
    wr(a[7:0], d);
   end else if (tok == "R") begin
    r = $fscanf(fin, "%h %h %h %h %h %h %h %h", w[0], w[1], w[2], w[3], w[4], w[5], w[6], w[7]);
    @(negedge clk);
    geo_fire = 1;
    geo_record = {w[7], w[6], w[5], w[4], w[3], w[2], w[1], w[0]};
    @(negedge clk);
    geo_fire = 0;
    for (i = 1; i < gap; i = i + 1) @(negedge clk);
   end else if (tok == "D") begin
    @(negedge clk); pkt_discard = 1; @(negedge clk); pkt_discard = 0;
   end else if (tok == "C") begin
    rd(8'h50, prev_cnt);
    @(negedge clk); pkt_commit = 1; @(negedge clk); pkt_commit = 0;
    i = 0;
    rd(8'h50, a);
    while (a == prev_cnt && i < 4000000) begin @(negedge clk); i = i + 1; rd(8'h50, a); end
    if (a == prev_cnt) begin $display("TIMEOUT packet %0d", packets); $finish; end
    dump;
    packets = packets + 1;
   end
  end
  $display("DONE %0d packets", packets);
  $fclose(fout);
  $finish;
 end
endmodule
