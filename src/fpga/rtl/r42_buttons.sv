`timescale 1ns/1ps
// R42: asynchronous active-high buttons; event counters never clear on read.
module r42_buttons #(parameter [31:0] DEFAULT_DEBOUNCE_CYCLES=32'd2000000)(
 input wire clk,input wire resetn,input wire [3:0] btn,
 input wire reg_we,input wire [7:0] reg_waddr,input wire [31:0] reg_wdata,
 input wire [3:0] reg_wstrb,input wire [7:0] reg_raddr,output reg [31:0] reg_rdata,
 output reg [3:0] level,output wire [31:0] counts
);
 (* ASYNC_REG="TRUE", SHREG_EXTRACT="NO" *) reg [3:0] sync1,sync2;
 reg [31:0] debounce_cycles;
 reg [31:0] stable_count[0:3];
 reg [7:0] press_count[0:3];
 integer k,b;
 wire config_write=reg_we && reg_waddr==8'h0c && (|reg_wstrb);
 assign counts={press_count[3],press_count[2],press_count[1],press_count[0]};
 always @* begin
  case(reg_raddr)
   8'h00:reg_rdata=32'h52343242;
   8'h04:reg_rdata={28'b0,level};
   8'h08:reg_rdata=counts;
   8'h0c:reg_rdata=debounce_cycles;
   default:reg_rdata=0;
  endcase
 end
 always @(posedge clk) begin
  if(!resetn)begin sync1<=0;sync2<=0;end
  else begin sync1<=btn;sync2<=sync1;end
 end
 always @(posedge clk)begin
  if(!resetn)begin
   debounce_cycles<=DEFAULT_DEBOUNCE_CYCLES;level<=0;
   for(k=0;k<4;k=k+1)begin stable_count[k]<=0;press_count[k]<=0;end
  end else begin
   if(config_write)for(b=0;b<4;b=b+1)if(reg_wstrb[b])debounce_cycles[b*8+:8]<=reg_wdata[b*8+:8];
   for(k=0;k<4;k=k+1)begin
    // Reconfiguration restarts qualification; 0 and 1 both mean one stable cycle.
    if(config_write || sync2[k]==level[k])stable_count[k]<=0;
    else if(debounce_cycles<=1 || stable_count[k]>=debounce_cycles-1)begin
     stable_count[k]<=0;level[k]<=sync2[k];
     if(sync2[k])press_count[k]<=press_count[k]+1'b1;
    end else stable_count[k]<=stable_count[k]+1'b1;
   end
  end
 end
endmodule
