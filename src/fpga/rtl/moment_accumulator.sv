`timescale 1ns/1ps
`default_nettype none
// 64 height bins, two epoch banks. Word-addressed BRAM avoids a wasteful
// 1024-bit-wide shallow memory. Updates serialize six genuine 32x32 products.
// Q16 first moments; Q32 second moments. Statistics never replace full points.
module moment_accumulator(
 input wire clk,rst,start,finish,
 input wire signed [31:0] bin_origin_q16,input wire [5:0] bin_shift,
 input wire [255:0] in_record,input wire in_valid,output wire in_ready,
 output wire ready,output wire busy,output reg done,output reg overflow,
 output reg [511:0] out_data,output reg out_valid,input wire out_ready,
 output reg [6:0] out_bin,output reg out_half,
 output reg [31:0] accepted_count,rejected_count
);
 localparam IDLE=0,INIT=1,ACCEPT=2,READ=3,READ_WAIT=4,PREP=5,MUL=6,MUL_WAIT=7,ADD=8,WRITE=9,DUMP=10,DUMP_WAIT=11,SEND=12,BIN_SUB=13,BIN_SHIFT=14,BIN_CHECK=15;
 reg [3:0] state;
 (* ram_style="block" *) reg [31:0] mem[0:4095];
 reg bank;
 reg [11:0] addr;
 reg [31:0] ram_q;
 reg [1023:0] work;
 reg [5:0] word_index;
 reg [5:0] bin;
 reg signed [31:0] x,y,z;
 // Latch a complete acceptance transaction before the bin arithmetic.
 // Each stage is genuine work: 33-bit subtract, barrel shift, bounds/address.
 reg signed [31:0] origin_latched;
 reg [5:0] shift_latched;
 reg record_qualified;
 reg signed [32:0] delta, shifted_delta;
 reg [2:0] product_index;
 reg signed [31:0] a,b;
 (* use_dsp="yes" *) reg signed [63:0] product;
 reg finishing;
 integer i;
 reg signed [95:0] second_selected;
 wire signed [95:0] second_sum=second_selected+$signed(product);
 always @* begin
  case(product_index)
   0:second_selected=work[351:256];
   1:second_selected=work[447:352];
   2:second_selected=work[543:448];
   3:second_selected=work[639:544];
   4:second_selected=work[735:640];
   default:second_selected=work[831:736];
  endcase
 end

 wire [11:0] base={bank,bin,5'b0};
 assign ready=state==ACCEPT;
 assign in_ready=state==ACCEPT&&!finish;
 assign busy=state!=IDLE&&state!=ACCEPT;
 always @(posedge clk)begin
  ram_q<=mem[addr];
  done<=0;
  if(rst)begin state<=IDLE;bank<=0;addr<=0;work<=0;word_index<=0;bin<=0;x<=0;y<=0;z<=0;delta<=0;shifted_delta<=0;origin_latched<=0;shift_latched<=0;record_qualified<=0;
   product_index<=0;a<=0;b<=0;product<=0;finishing<=0;out_data<=0;out_valid<=0;out_bin<=0;out_half<=0;
   accepted_count<=0;rejected_count<=0;overflow<=0;
  end else begin
   if(start&&state==IDLE)begin bank<=~bank;addr<={(~bank),11'b0};word_index<=0;bin<=0;
    accepted_count<=0;rejected_count<=0;overflow<=0;finishing<=0;state<=INIT;end
   case(state)
    INIT:begin
     mem[addr]<=0;
     if(addr[10:0]==2047)state<=ACCEPT;else addr<=addr+1'b1;
    end
    ACCEPT:begin
     if(finish)begin bin<=0;word_index<=0;out_half<=0;addr<={bank,11'b0};state<=DUMP;end
     else if(in_valid)begin
      x<=in_record[159:128];y<=in_record[191:160];z<=in_record[223:192];
      origin_latched<=bin_origin_q16;shift_latched<=bin_shift;
      record_qualified<=in_record[224]&&in_record[232];state<=BIN_SUB;
     end
    end
    BIN_SUB:begin
     delta<=$signed({z[31],z})-$signed({origin_latched[31],origin_latched});
     state<=BIN_SHIFT;
    end
    BIN_SHIFT:begin shifted_delta<=delta>>>shift_latched;state<=BIN_CHECK;end
    BIN_CHECK:begin
     if(record_qualified&&!delta[32]&&shifted_delta<33'sd64)begin
      bin<=shifted_delta[5:0];addr<={bank,shifted_delta[5:0],5'b0};word_index<=0;state<=READ;
     end else begin rejected_count<=rejected_count+1'b1;state<=ACCEPT;end
    end
    READ:state<=READ_WAIT;
    READ_WAIT:begin
     work<={ram_q,work[1023:32]};
     if(word_index==31)state<=PREP;
     else begin word_index<=word_index+1'b1;addr<=addr+1'b1;state<=READ;end
    end
    PREP:begin
     if(work[31:0]>=32'h7fffffff)begin overflow<=1;state<=ACCEPT;end
     else begin
      work[31:0]<=work[31:0]+1'b1;
      work[127:64]<=$signed(work[127:64])+$signed(x);
      work[191:128]<=$signed(work[191:128])+$signed(y);
      work[255:192]<=$signed(work[255:192])+$signed(z);
      if(work[31:0]==0)begin work[863:832]<=x;work[895:864]<=x;work[927:896]<=y;work[959:928]<=y;work[991:960]<=z;work[1023:992]<=z;end
      else begin
       if(x<$signed(work[863:832]))work[863:832]<=x;if(x>$signed(work[895:864]))work[895:864]<=x;
       if(y<$signed(work[927:896]))work[927:896]<=y;if(y>$signed(work[959:928]))work[959:928]<=y;
       if(z<$signed(work[991:960]))work[991:960]<=z;if(z>$signed(work[1023:992]))work[1023:992]<=z;
      end
      accepted_count<=accepted_count+1'b1;product_index<=0;state<=MUL;
     end
    end
    MUL:begin
     case(product_index)0:begin a<=x;b<=x;end 1:begin a<=y;b<=y;end 2:begin a<=z;b<=z;end
      3:begin a<=x;b<=y;end 4:begin a<=x;b<=z;end default:begin a<=y;b<=z;end endcase
     state<=MUL_WAIT;
    end
    MUL_WAIT:begin product<=a*b;state<=ADD;end
    ADD:begin
     case(product_index)
      0:work[351:256]<=second_sum;
      1:work[447:352]<=second_sum;
      2:work[543:448]<=second_sum;
      3:work[639:544]<=second_sum;
      4:work[735:640]<=second_sum;
      default:work[831:736]<=second_sum;
     endcase
     if(product_index==5)begin word_index<=0;addr<=base;state<=WRITE;end
     else begin product_index<=product_index+1'b1;state<=MUL;end
    end
    WRITE:begin
     mem[addr]<=work[31:0];
     work<={32'b0,work[1023:32]};
     if(word_index==31)state<=ACCEPT;else begin word_index<=word_index+1'b1;addr<=addr+1'b1;end
    end
    DUMP:state<=DUMP_WAIT;
    DUMP_WAIT:begin
     out_data<={ram_q,out_data[511:32]};
     if(word_index[3:0]==15)begin out_valid<=1;out_bin<={1'b0,bin};state<=SEND;end
     else begin word_index<=word_index+1'b1;addr<=addr+1'b1;state<=DUMP;end
    end
    SEND:if(out_ready)begin
     out_valid<=0;
     if(out_half)begin
      if(bin==63)begin done<=1;state<=IDLE;end
      else begin bin<=bin+1'b1;word_index<=0;out_half<=0;addr<=addr+1'b1;state<=DUMP;end
     end else begin out_half<=1;word_index<=16;addr<=addr+1'b1;state<=DUMP;end
    end
    default:begin end
   endcase
  end
 end
endmodule
`default_nettype wire
