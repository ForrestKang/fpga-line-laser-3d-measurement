`timescale 1ns/1ps
// Passive Raw AXIS observer. No output influences acquisition or DMA ready.
// All inputs and state are in system_clk. FIFO is bounded; faults invalidate
// the snapshot and clear SHA words. The host must then use CPU fallback.
module raw_integrity_r24 #(parameter integer FIFO_WORDS=4096)(
 input wire clk, reset, enable, clear, start, abort_frame, capture_complete,
 input wire [31:0] request_id, input wire [15:0] epoch,
 input wire [31:0] expected_bytes,
 input wire [31:0] raw_data, input wire [3:0] raw_keep,
 input wire raw_fire, raw_last,
 output wire [31:0] status, output reg [31:0] result_id,
 output reg [15:0] result_epoch, output reg [31:0] byte_count,
 output reg [255:0] digest, output reg [7:0] pixel_min,pixel_max,
 output reg [31:0] pixel_sum,saturated_count, output reg [31:0] cycles
);
 // V2: isolate the passive tap from upstream asynchronously reset FFs.
 // These registers have synchronous reset only. No signal drives Raw ready.
 // Discard pending beats at every transaction boundary/abort.
 reg raw_fire_q,raw_last_q;
 reg [31:0] raw_data_q;
 reg [3:0] raw_keep_q;
 always @(posedge clk) begin
  if(reset || clear || !enable || abort_frame || start) begin
   raw_fire_q<=0;raw_last_q<=0;raw_data_q<=0;raw_keep_q<=0;
  end else begin
   raw_fire_q<=raw_fire;raw_last_q<=raw_last;
   raw_data_q<=raw_data;raw_keep_q<=raw_keep;
  end
 end
 localparam AW=$clog2(FIFO_WORDS);
 localparam FETCH=0, LOAD=1, BYTES=2, PAD=3, ROUND0=4, ROUND1=5, ACCUM=6, IDLE=7;
 (* ram_style="block" *) reg [34:0] fifo [0:FIFO_WORDS-1];
 reg [34:0] fifo_q;
 reg [AW-1:0] wp,rp;
 reg [AW:0] occupancy;
 reg busy,ready,overflow,aborted,protocol_error,sha_done,committed;
 reg [3:0] state,resume_state;
 reg [31:0] word_buf,consumed,expected;
 reg [2:0] remaining;
 reg word_last;
 reg [6:0] padding_left;
 reg first_pad;
 reg [63:0] bit_length;
 reg [5:0] pos,round;
 reg [31:0] w[0:15];
 reg [31:0] h0,h1,h2,h3,h4,h5,h6,h7;
 reg [31:0] a,b,c,d,e,f,g,h,t1,t2,next_w;
 wire [2:0] incoming_bytes = raw_keep_q==4'hf ? 3'd4 : raw_keep_q==7 ? 3'd3 : raw_keep_q==3 ? 3'd2 : 3'd1;
 wire legal_keep = raw_keep_q==15 || (raw_last_q && (raw_keep_q==1 || raw_keep_q==3 || raw_keep_q==7));
 wire push = busy && !sha_done && raw_fire_q;
 wire pop = busy && state==LOAD;
 wire [7:0] pad_byte = first_pad ? 8'h80 : padding_left<=8 ? (bit_length >> ((padding_left-1)*8)) : 8'd0;
 wire [7:0] current_byte = state==BYTES ? word_buf[7:0] : pad_byte;
 assign status={26'd0,enable,protocol_error,aborted,overflow,ready,busy};
 function [31:0] rotr(input [31:0] x,input integer n); rotr=(x>>n)|(x<<(32-n)); endfunction
 function [31:0] kval(input [5:0] n); begin case(n)
 6'd0: kval=32'h428a2f98;
 6'd1: kval=32'h71374491;
 6'd2: kval=32'hb5c0fbcf;
 6'd3: kval=32'he9b5dba5;
 6'd4: kval=32'h3956c25b;
 6'd5: kval=32'h59f111f1;
 6'd6: kval=32'h923f82a4;
 6'd7: kval=32'hab1c5ed5;
 6'd8: kval=32'hd807aa98;
 6'd9: kval=32'h12835b01;
 6'd10: kval=32'h243185be;
 6'd11: kval=32'h550c7dc3;
 6'd12: kval=32'h72be5d74;
 6'd13: kval=32'h80deb1fe;
 6'd14: kval=32'h9bdc06a7;
 6'd15: kval=32'hc19bf174;
 6'd16: kval=32'he49b69c1;
 6'd17: kval=32'hefbe4786;
 6'd18: kval=32'h0fc19dc6;
 6'd19: kval=32'h240ca1cc;
 6'd20: kval=32'h2de92c6f;
 6'd21: kval=32'h4a7484aa;
 6'd22: kval=32'h5cb0a9dc;
 6'd23: kval=32'h76f988da;
 6'd24: kval=32'h983e5152;
 6'd25: kval=32'ha831c66d;
 6'd26: kval=32'hb00327c8;
 6'd27: kval=32'hbf597fc7;
 6'd28: kval=32'hc6e00bf3;
 6'd29: kval=32'hd5a79147;
 6'd30: kval=32'h06ca6351;
 6'd31: kval=32'h14292967;
 6'd32: kval=32'h27b70a85;
 6'd33: kval=32'h2e1b2138;
 6'd34: kval=32'h4d2c6dfc;
 6'd35: kval=32'h53380d13;
 6'd36: kval=32'h650a7354;
 6'd37: kval=32'h766a0abb;
 6'd38: kval=32'h81c2c92e;
 6'd39: kval=32'h92722c85;
 6'd40: kval=32'ha2bfe8a1;
 6'd41: kval=32'ha81a664b;
 6'd42: kval=32'hc24b8b70;
 6'd43: kval=32'hc76c51a3;
 6'd44: kval=32'hd192e819;
 6'd45: kval=32'hd6990624;
 6'd46: kval=32'hf40e3585;
 6'd47: kval=32'h106aa070;
 6'd48: kval=32'h19a4c116;
 6'd49: kval=32'h1e376c08;
 6'd50: kval=32'h2748774c;
 6'd51: kval=32'h34b0bcb5;
 6'd52: kval=32'h391c0cb3;
 6'd53: kval=32'h4ed8aa4a;
 6'd54: kval=32'h5b9cca4f;
 6'd55: kval=32'h682e6ff3;
 6'd56: kval=32'h748f82ee;
 6'd57: kval=32'h78a5636f;
 6'd58: kval=32'h84c87814;
 6'd59: kval=32'h8cc70208;
 6'd60: kval=32'h90befffa;
 6'd61: kval=32'ha4506ceb;
 6'd62: kval=32'hbef9a3f7;
 6'd63: kval=32'hc67178f2;
 endcase end endfunction
 // Synchronous read, held pointer during byte serialization.
 always @(posedge clk) fifo_q <= fifo[rp];
 always @(posedge clk) if(push && occupancy<FIFO_WORDS) fifo[wp] <= {raw_last_q,incoming_bytes[1:0],raw_data_q};
 integer i;
 always @(posedge clk) begin
  if(reset || clear || !enable || abort_frame) begin
   wp<=0;rp<=0;occupancy<=0;busy<=0;ready<=0;overflow<=0;
   aborted<=abort_frame && enable; protocol_error<=0;sha_done<=0;committed<=0;
   result_id<=0;result_epoch<=0;byte_count<=0;digest<=0;
   pixel_min<=0;pixel_max<=0;pixel_sum<=0;saturated_count<=0;cycles<=0;
   state<=IDLE;consumed<=0;pos<=0;remaining<=0;padding_left<=0;
  end else if(start) begin
   wp<=0;rp<=0;occupancy<=0;ready<=0;digest<=0;byte_count<=0;
   result_id<=request_id;result_epoch<=epoch;expected<=expected_bytes;
   overflow<=0;aborted<=0;protocol_error<=busy;busy<=!busy;
   sha_done<=0;committed<=0;consumed<=0;pos<=0;remaining<=0;cycles<=0;
   pixel_min<=255;pixel_max<=0;pixel_sum<=0;saturated_count<=0;
   state<=FETCH; h0<=32'h6a09e667;h1<=32'hbb67ae85;h2<=32'h3c6ef372;h3<=32'ha54ff53a;
   h4<=32'h510e527f;h5<=32'h9b05688c;h6<=32'h1f83d9ab;h7<=32'h5be0cd19;
   for(i=0;i<16;i=i+1) w[i]<=0;
   // Empty message is supported for FIPS known answers (no AXIS beat).
   if(expected_bytes==0) begin pixel_min<=0;state<=PAD;padding_left<=64;first_pad<=1;bit_length<=0;end
  end else if(busy) begin
   cycles<=cycles+1;
   if(capture_complete) committed<=1;
   if(sha_done && (committed || capture_complete)) begin ready<=1;busy<=0;end
   if(push) begin wp<=wp+1'b1;byte_count<=byte_count+incoming_bytes;end
   if(pop) rp<=rp+1'b1;
   case({push,pop})
    2'b10: occupancy<=occupancy+1'b1;
    2'b01: occupancy<=occupancy-1'b1;
    default: occupancy<=occupancy;
   endcase
   case(state)
    FETCH: if(occupancy!=0) state<=LOAD;
    LOAD: begin
     word_buf<=fifo_q[31:0];remaining<=fifo_q[33:32]==0?4:{1'b0,fifo_q[33:32]};
     word_last<=fifo_q[34];state<=BYTES;
    end
    BYTES,PAD: begin
     w[pos[5:2]] <= {w[pos[5:2]][23:0],current_byte};
     pos<=pos+1'b1;
     if(state==BYTES) begin
      consumed<=consumed+1;word_buf<=word_buf>>8;remaining<=remaining-1;
      if(current_byte<pixel_min) pixel_min<=current_byte;
      if(current_byte>pixel_max) pixel_max<=current_byte;
      pixel_sum<=pixel_sum+current_byte;
      if(current_byte==255) saturated_count<=saturated_count+1;
      if(remaining==1) begin
       if(word_last) begin
        padding_left<= ((consumed[5:0]+1)&63)<56 ? 64-((consumed[5:0]+1)&63) : 128-((consumed[5:0]+1)&63);
        first_pad<=1; bit_length<=({32'd0,consumed}+1)<<3;
        state<=PAD;
       end else state<=FETCH;
      end
     end else begin padding_left<=padding_left-1;first_pad<=0;end
     if(pos==63) begin
      state<=ROUND0;round<=0;
      resume_state<=state==PAD ? PAD : remaining==1 ? (word_last?PAD:FETCH) : BYTES;
      a<=h0;b<=h1;c<=h2;d<=h3;e<=h4;f<=h5;g<=h6;h<=h7;
     end
    end
    ROUND0: begin
     t1<=h+(rotr(e,6)^rotr(e,11)^rotr(e,25))+((e&f)^((~e)&g))+kval(round)+w[0];
     t2<=(rotr(a,2)^rotr(a,13)^rotr(a,22))+((a&b)^(a&c)^(b&c));
     next_w<=(rotr(w[14],17)^rotr(w[14],19)^(w[14]>>10))+w[9]+(rotr(w[1],7)^rotr(w[1],18)^(w[1]>>3))+w[0];
     state<=ROUND1;
    end
    ROUND1: begin
     h<=g;g<=f;f<=e;e<=d+t1;d<=c;c<=b;b<=a;a<=t1+t2;
     for(i=0;i<15;i=i+1) w[i]<=w[i+1];w[15]<=next_w;
     if(round==63) state<=ACCUM; else begin round<=round+1'b1;state<=ROUND0;end
    end
    ACCUM: begin
     h0<=h0+a;h1<=h1+b;h2<=h2+c;h3<=h3+d;h4<=h4+e;h5<=h5+f;h6<=h6+g;h7<=h7+h;
     state<=resume_state;
     if(resume_state==PAD && padding_left==0) begin
      digest<={h0+a,h1+b,h2+c,h3+d,h4+e,h5+f,h6+g,h7+h};
      sha_done<=1;state<=IDLE;
     end
    end
    default: state<=IDLE;
   endcase
   // Faults dominate all normal transitions. No partial/stale digest is valid.
   if(push && (occupancy==FIFO_WORDS || !legal_keep ||
       byte_count+incoming_bytes>expected ||
       (raw_last_q != (byte_count+incoming_bytes==expected)))) begin
    busy<=0;ready<=0;digest<=0;state<=IDLE;
    overflow<=occupancy==FIFO_WORDS;
    protocol_error<= !legal_keep || byte_count+incoming_bytes>expected ||
       (raw_last_q != (byte_count+incoming_bytes==expected));
   end
  end
 end
endmodule
