`timescale 1ns/1ps
`default_nettype none
// R41 laser auto-exposure (PL). Watches the Raw stream the capture IP already sends to raw_dma
// (4 pixels per beat, byte 0 first) and, after each frame, steps the MT9V034 coarse exposure
// (register 0x0B) through the existing SCCB master:
//   saturated (>=250) count > sat_limit      -> rows - max(1, rows/4)   (x0.75)
//   else frame peak < target_low             -> rows + max(1, rows/8)   (x1.125)
//   else                                       hold
// clamped to [min_rows, max_rows]. After a successful write it waits settle_frames frames
// before deciding again (the sensor applies a new exposure one to two frames later).
// Disabled by default; software turns the sensor's own AEC off and loads `rows` first.
module r41_laser_ae(
 input  wire        clk,
 input  wire        rst,
 // Raw stream tap (observe only)
 input  wire [31:0] raw_data,
 input  wire        raw_fire,
 input  wire        raw_last,
 // Configuration (software registers)
 input  wire        enable,
 input  wire        clear_stats,
 input  wire [3:0]  settle_frames,
 input  wire [15:0] min_rows,
 input  wire [15:0] max_rows,
 input  wire [19:0] sat_limit,
 input  wire [7:0]  target_low,
 input  wire        rows_load,          // software write of the current exposure (only while disabled)
 input  wire [15:0] rows_load_value,
 // Status
 output reg  [15:0] rows,
 output reg  [19:0] last_sat,
 output reg  [7:0]  last_peak,
 output reg  [15:0] frames,
 output reg  [31:0] updates,
 output reg  [15:0] errors,
 output reg         last_error,
 output wire        active,             // an exposure write is requested or on the bus
 // SCCB request (the IP grants it only when its master is idle and software is not starting one)
 output reg         req,
 output wire [7:0]  req_reg,
 output reg  [15:0] req_data,
 input  wire        grant,
 input  wire        done,
 input  wire        fail
);
 assign req_reg = 8'h0B;
 reg busy;
 assign active = req | busy;

 // ---------------- per-frame statistics (stage 1: per-beat counts, stage 2: accumulate)
 wire [7:0] p0 = raw_data[7:0], p1 = raw_data[15:8], p2 = raw_data[23:16], p3 = raw_data[31:24];
 wire [2:0] beat_sat = (p0 >= 8'd250) + (p1 >= 8'd250) + (p2 >= 8'd250) + (p3 >= 8'd250);
 wire [7:0] m01 = p0 > p1 ? p0 : p1;
 wire [7:0] m23 = p2 > p3 ? p2 : p3;
 wire [7:0] beat_peak = m01 > m23 ? m01 : m23;
 reg        s_fire, s_last;
 reg [2:0]  s_sat;
 reg [7:0]  s_peak;
 reg [19:0] acc_sat;
 reg [7:0]  acc_peak;
 reg        frame_end;
 wire [19:0] sum_sat = (acc_sat == 20'hfffff || acc_sat + s_sat < acc_sat) ? 20'hfffff : acc_sat + s_sat;
 wire [7:0]  max_peak = s_peak > acc_peak ? s_peak : acc_peak;

 // ---------------- decision
 wire [15:0] step_dn = (rows >> 2) == 0 ? 16'd1 : (rows >> 2);
 wire [15:0] step_up = (rows >> 3) == 0 ? 16'd1 : (rows >> 3);
 wire [16:0] up_raw = {1'b0, rows} + {1'b0, step_up};
 wire [15:0] dn_val = rows > step_dn ? rows - step_dn : 16'd1;
 wire [15:0] up_val = up_raw > {1'b0, max_rows} ? max_rows : up_raw[15:0];
 wire [15:0] dn_clamped = dn_val < min_rows ? min_rows : dn_val;
 wire [15:0] next_rows = last_sat > sat_limit ? dn_clamped :
                         last_peak < target_low ? up_val : rows;
 reg [3:0] settle;
 reg       decide;
 // b13: next_rows and the change test are registered one cycle before the request (timing);
 // rows cannot change in between, since a decision needs an idle, request-free master.
 reg       go;
 reg       change_q;
 reg [15:0] next_q;

 always @(posedge clk) begin
  if (rst) begin
   rows <= 16'd480; last_sat <= 0; last_peak <= 0; frames <= 0; updates <= 0; errors <= 0; last_error <= 0;
   req <= 0; req_data <= 0; busy <= 0;
   s_fire <= 0; s_last <= 0; s_sat <= 0; s_peak <= 0; acc_sat <= 0; acc_peak <= 0; frame_end <= 0;
   settle <= 0; decide <= 0; go <= 0; change_q <= 0; next_q <= 0;
  end else begin
   // stage 1
   s_fire <= raw_fire; s_last <= raw_fire && raw_last; s_sat <= beat_sat; s_peak <= beat_peak;
   // stage 2
   frame_end <= 0;
   if (s_fire) begin
    if (s_last) begin
     last_sat <= sum_sat; last_peak <= max_peak; frames <= frames + 1'b1;
     acc_sat <= 0; acc_peak <= 0; frame_end <= 1;
    end else begin
     acc_sat <= sum_sat; acc_peak <= max_peak;
    end
   end
   if (clear_stats) begin
    frames <= 0; updates <= 0; errors <= 0; last_error <= 0;
   end
   // stage 3: one decision per finished frame, after the settle window
   decide <= 0;
   if (frame_end) begin
    if (settle != 0) settle <= settle - 1'b1;
    else decide <= enable;
   end
   go <= decide && enable && !req && !busy;
   next_q <= next_rows; change_q <= next_rows != rows;
   if (go && enable && !req && !busy && change_q) begin
    req <= 1; req_data <= next_q;
   end
   if (!enable && !busy) req <= 0;             // a not yet granted request is withdrawn
   if (grant) begin req <= 0; busy <= 1; end
   if (done && busy) begin
    busy <= 0;
    if (fail) begin
     errors <= errors + 1'b1; last_error <= 1;
    end else begin
     rows <= req_data; updates <= updates + 1'b1; last_error <= 0; settle <= settle_frames;
    end
   end
   if (rows_load && !enable && !busy && !req) rows <= rows_load_value;
  end
 end
endmodule
`default_nettype wire
