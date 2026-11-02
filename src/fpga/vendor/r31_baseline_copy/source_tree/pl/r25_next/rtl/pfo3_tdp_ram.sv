`timescale 1ns/1ps
// Two independent synchronous ports. Callers arbitrate init and normal writes
// onto these same two ports; there is no hidden third initialization port.
module pfo3_tdp_ram #(parameter WIDTH=16,DEPTH=7680)(
 input wire clk,
 input wire [12:0] aa,ab,
 input wire wa,wb,
 input wire [WIDTH-1:0] da,db,
 output reg [WIDTH-1:0] qa,qb
);
 (* ram_style="block" *) reg [WIDTH-1:0] mem[0:DEPTH-1];
 always @(posedge clk)begin if(wa)mem[aa]<=da;qa<=mem[aa];end
 always @(posedge clk)begin if(wb)mem[ab]<=db;qb<=mem[ab];end
endmodule
