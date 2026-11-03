`timescale 1ns/1ps
`default_nettype none
// live measurement live measurement statistics over the PL geometry records of one PFO3 packet.
// Read-only snoop of geo_out_record (same source and commit rule as point_cloud_renderer): points
// count only for packets whose footer leaves without error; start/abort/error discard them.
// Record words: [0] id, [1..3] camera X/Y/Z Q16 mm, [4..6] axis X/Y/Z Q16 mm, [7] flags
// (bit8 valid, bit1 preview, bit0 measurement).
// Per committed packet (double buffered, software reads the last complete frame):
//   N, camera Z min/max/sum + median (256-bin histogram), camera X/Y min/max,
//   axis Z min/max, and 32 axis-height bands: count, min/max/sum of r^2 = ax^2 + ay^2 (Q32).
// Registers (offset within the 0x500 window, 32-bit):
//  00 magic 0x5234314D         04 ctrl: b0 enable, b1 preview points (else measurement)
//  08 z_origin (Q16)       0C z_shift [4:0] (bin = 2^shift Q16 units)
//  10 h_origin (Q16)       14 h_shift [4:0]          18 band select [4:0]
//  20 committed packets    24 N                       28 z_min   2C z_max
//  30 z_sum[31:0]          34 z_sum[47:32] (sign-extended)
//  38 median: b31 valid, [7:0] bin        3C in-range M   40 below   44 above
//  48 x_min 4C x_max 50 y_min 54 y_max    58 axis z min   5C axis z max
//  60 points dropped while busy (cumulative)   64 band count
//  68/6C band r2 max lo/hi   70/74 band r2 min lo/hi   78/7C band r2 sum lo/hi   80 points outside bands
//
// b13: the histogram and the band table are block RAM (registered reads) instead of LUTRAM.
//  * Point pipeline p0 capture -> p1 squares/bins -> p2 r^2 + memory read -> p3 read-modify-write.
//    A p3 write and the next point's read can meet at the same edge; the next p3 then takes the
//    value just written (one-deep forwarding), so back-to-back points into one bin stay exact.
//  * A commit/discard waits until p0..p3 are empty (points taken before it are all counted);
//    points arriving after it, until the scan ends, are dropped and counted as before.
//  * Histogram (256 x 16, simple dual port): the scan reads bin k and clears it one cycle later.
//  * Band table (64 x 208 = 2 banks x 32 bands, true dual port): port A = pipeline reads and the
//    bank clears, port B = pipeline writes, otherwise a continuous read of the selected result
//    band (address taken from the 0x18 write data in its own cycle, so a read issued a cycle
//    after the select already returns the new band).
module profile_statistics(
 input  wire         clk,
 input  wire         rst,
 input  wire         geo_fire,
 input  wire [255:0] geo_record,
 input  wire         pkt_commit,
 input  wire         pkt_discard,
 input  wire         reg_we,
 input  wire [7:0]   reg_waddr,
 input  wire [31:0]  reg_wdata,
 input  wire [7:0]   reg_raddr,
 output reg  [31:0]  reg_rdata
);
 localparam [31:0] MAGIC = 32'h5234314D;
 reg        enable, use_preview;
 reg signed [31:0] z_origin, h_origin;
 reg [4:0]  z_shift, h_shift, band_sel;

 // ---------------- end-of-packet scan / clear
 localparam S_RUN = 0, S_SCAN = 1, S_CLEAR = 2;
 reg [1:0] s_state;
 reg [8:0] s_idx;
 reg [15:0] s_cum, s_target;
 reg s_found, s_commit;
 reg pend, pend_commit;                     // packet end seen, waiting for the pipeline to drain

 // ---------------- point pipeline
 wire [31:0] flags = geo_record[255:224];
 wire take = geo_fire && enable && flags[8] && (use_preview ? flags[1] : flags[0]);
 wire accept = s_state == S_RUN && !pend;
 reg  p0_v; reg signed [31:0] p0_cx, p0_cy, p0_cz, p0_ax, p0_ay, p0_az;
 reg signed [32:0] p0_dz, p0_dh;       // offsets taken at capture, so stage 1 only shifts and compares
 (* use_dsp = "yes" *) reg signed [63:0] p1_ax2, p1_ay2;
 reg  p1_v; reg [8:0] p1_zbin; reg [1:0] p1_zcls; reg [5:0] p1_hbin;
 reg  p2_v; reg [63:0] p2_r2; reg [7:0] p2_zbin; reg [1:0] p2_zcls; reg [5:0] p2_hbin;
 reg  p3_v; reg [63:0] p3_r2; reg [7:0] p3_zbin; reg [1:0] p3_zcls; reg [5:0] p3_hbin;
 wire drained = !p0_v && !p1_v && !p2_v && !p3_v;
 // z bin: 0 below, 1 inside (bin index), 2 above
 wire signed [32:0] dz = p0_dz;
 wire signed [32:0] dh = p0_dh;
 wire signed [32:0] zq = dz >>> z_shift;
 wire signed [32:0] hq = dh >>> h_shift;

 // ---------------- working accumulators (current packet)
 reg [15:0] w_n, w_below, w_above, w_hout;
 reg signed [31:0] w_zmin, w_zmax, w_xmin, w_xmax, w_ymin, w_ymax, w_hmin, w_hmax;
 reg signed [47:0] w_zsum;
 // ---------------- results (last committed packet)
 reg [31:0] r_seq;
 reg [15:0] r_n, r_below, r_above, r_hout, r_m;
 reg signed [31:0] r_zmin, r_zmax, r_xmin, r_xmax, r_ymin, r_ymax, r_hmin, r_hmax;
 reg signed [47:0] r_zsum;
 reg [7:0] r_median; reg r_median_valid;
 reg [31:0] dropped;
 reg wb;                                    // working band bank (results use !wb)

 // ---------------- z histogram: 256 x 16 block RAM, one read and one write port
 (* ram_style = "block" *) reg [15:0] hist [0:255];
 reg [7:0]  h_ra, h_wa;
 reg [15:0] h_do, h_wd;
 reg        h_we;
 reg        hf_v; reg [7:0] hf_a; reg [15:0] hf_d;      // last write (forwarding)
 wire [15:0] h_cur = (hf_v && hf_a == p3_zbin) ? hf_d : h_do;
 always @(*) begin
  h_ra = s_state == S_RUN ? p2_zbin : s_idx[7:0];
  h_we = 0; h_wa = p3_zbin; h_wd = h_cur + 1'b1;
  if (s_state == S_RUN) h_we = p3_v && p3_zcls == 1;
  else if (s_state == S_SCAN) begin h_we = s_idx != 0; h_wa = s_idx[7:0] - 1'b1; h_wd = 0; end
  else begin h_we = 1; h_wa = s_idx[7:0]; h_wd = 0; end
 end
 always @(posedge clk) begin
  if (h_we) hist[h_wa] <= h_wd;
  h_do <= hist[h_ra];
 end

 // ---------------- band table: 64 x {count 16, r2 max 64, r2 min 64, r2 sum 64}, true dual port
 (* ram_style = "block" *) reg [207:0] band [0:63];
 reg [5:0]   ba_a, bb_a;
 reg         ba_we, bb_we;
 reg [207:0] ba_do, bb_do, bb_wd;
 reg         bf_v; reg [5:0] bf_a; reg [207:0] bf_d;     // last pipeline write (forwarding)
 reg         bb_rd_q;                                   // port B output holds the selected band
 reg [207:0] b_snap;
 wire [5:0]   b_p3a = {wb, p3_hbin[4:0]};
 wire [207:0] b_cur = (bf_v && bf_a == b_p3a) ? bf_d : ba_do;
 wire [15:0]  c_cnt = b_cur[207:192];
 wire [63:0]  c_max = b_cur[191:128], c_min = b_cur[127:64], c_sum = b_cur[63:0];
 wire [207:0] b_new = {c_cnt + 1'b1,
                       (c_cnt == 0 || p3_r2 > c_max) ? p3_r2 : c_max,
                       (c_cnt == 0 || p3_r2 < c_min) ? p3_r2 : c_min,
                       c_sum + p3_r2};
 wire [5:0]   b_sel = (reg_we && reg_waddr == 8'h18) ? {~wb, reg_wdata[4:0]} : {~wb, band_sel};
 always @(*) begin
  ba_we = 0; ba_a = {wb, p2_hbin[4:0]};
  if (s_state == S_SCAN && s_idx < 32) begin ba_we = 1; ba_a = {wb, s_idx[4:0]}; end
  else if (s_state == S_CLEAR && s_idx < 64) begin ba_we = 1; ba_a = s_idx[5:0]; end
  bb_we = s_state == S_RUN && p3_v && !p3_hbin[5];
  bb_a = bb_we ? b_p3a : b_sel;
  bb_wd = b_new;
 end
 always @(posedge clk) begin
  if (ba_we) band[ba_a] <= 208'd0;
  ba_do <= band[ba_a];
 end
 always @(posedge clk) begin
  if (bb_we) band[bb_a] <= bb_wd;
  bb_do <= band[bb_a];
 end
 wire [207:0] b_out = bb_rd_q ? bb_do : b_snap;

 always @(posedge clk) begin
  if (rst) begin
   enable <= 0; use_preview <= 0; z_origin <= 0; z_shift <= 18; h_origin <= 0; h_shift <= 18; band_sel <= 0;
   p0_v <= 0; p1_v <= 0; p2_v <= 0; p3_v <= 0; wb <= 0; s_state <= S_CLEAR; s_idx <= 0; s_commit <= 0;
   s_cum <= 0; s_target <= 0; s_found <= 0; pend <= 0; pend_commit <= 0;
   hf_v <= 0; bf_v <= 0; bb_rd_q <= 0; b_snap <= 0;
   r_seq <= 0; r_n <= 0; r_below <= 0; r_above <= 0; r_hout <= 0; r_m <= 0; r_median <= 0; r_median_valid <= 0;
   r_zmin <= 0; r_zmax <= 0; r_xmin <= 0; r_xmax <= 0; r_ymin <= 0; r_ymax <= 0; r_hmin <= 0; r_hmax <= 0;
   r_zsum <= 0; dropped <= 0;
   w_n <= 0; w_below <= 0; w_above <= 0; w_hout <= 0; w_zsum <= 0;
   w_zmin <= 32'sh7fffffff; w_zmax <= 32'sh80000000; w_xmin <= 32'sh7fffffff; w_xmax <= 32'sh80000000;
   w_ymin <= 32'sh7fffffff; w_ymax <= 32'sh80000000; w_hmin <= 32'sh7fffffff; w_hmax <= 32'sh80000000;
  end else begin
   // software registers
   if (reg_we) case (reg_waddr)
    8'h04: begin enable <= reg_wdata[0]; use_preview <= reg_wdata[1]; end
    8'h08: z_origin <= reg_wdata;
    8'h0C: z_shift <= reg_wdata[4:0];
    8'h10: h_origin <= reg_wdata;
    8'h14: h_shift <= reg_wdata[4:0];
    8'h18: band_sel <= reg_wdata[4:0];
    default: begin end
   endcase
   // forwarding / band read-out bookkeeping
   hf_v <= h_we && s_state == S_RUN; hf_a <= h_wa; hf_d <= h_wd;
   bf_v <= bb_we; bf_a <= bb_a; bf_d <= bb_wd;
   bb_rd_q <= !bb_we;
   if (bb_rd_q) b_snap <= bb_do;
   // stage 0: capture the record (points arriving after a packet end, until its scan is over,
   // are dropped)
   p0_v <= take && accept;
   if (take && !accept) dropped <= dropped + 1'b1;
   p0_cx <= geo_record[63:32]; p0_cy <= geo_record[95:64]; p0_cz <= geo_record[127:96];
   p0_ax <= geo_record[159:128]; p0_ay <= geo_record[191:160]; p0_az <= geo_record[223:192];
   p0_dz <= $signed({geo_record[127], geo_record[127:96]}) - $signed({z_origin[31], z_origin});
   p0_dh <= $signed({geo_record[223], geo_record[223:192]}) - $signed({h_origin[31], h_origin});
   // stage 1: squares, bins, camera X/Y and axis Z extremes, z sum
   p1_v <= p0_v;
   p1_ax2 <= p0_ax * p0_ax; p1_ay2 <= p0_ay * p0_ay;
   p1_zcls <= dz < 0 ? 2'd0 : (zq > 255 ? 2'd2 : 2'd1);
   p1_zbin <= zq[8:0];
   p1_hbin <= (dh < 0 || hq > 31) ? 6'd32 : {1'b0, hq[4:0]};
   if (p0_v) begin
    w_n <= w_n + 1'b1;
    w_zsum <= w_zsum + p0_cz;
    if (p0_cz < w_zmin) w_zmin <= p0_cz;
    if (p0_cz > w_zmax) w_zmax <= p0_cz;
    if (p0_cx < w_xmin) w_xmin <= p0_cx;
    if (p0_cx > w_xmax) w_xmax <= p0_cx;
    if (p0_cy < w_ymin) w_ymin <= p0_cy;
    if (p0_cy > w_ymax) w_ymax <= p0_cy;
    if (p0_az < w_hmin) w_hmin <= p0_az;
    if (p0_az > w_hmax) w_hmax <= p0_az;
   end
   // stage 2: r^2, memory reads issued (addresses above)
   p2_v <= p1_v; p2_r2 <= p1_ax2 + p1_ay2; p2_zbin <= p1_zbin[7:0]; p2_zcls <= p1_zcls; p2_hbin <= p1_hbin;
   if (p2_v) begin
    if (p2_zcls == 0) w_below <= w_below + 1'b1;
    else if (p2_zcls == 2) w_above <= w_above + 1'b1;
    if (p2_hbin[5]) w_hout <= w_hout + 1'b1;
   end
   // stage 3: read-modify-write (write ports above)
   p3_v <= p2_v; p3_r2 <= p2_r2; p3_zbin <= p2_zbin; p3_zcls <= p2_zcls; p3_hbin <= p2_hbin;
   // packet end
   case (s_state)
    S_RUN: begin
     if ((pkt_commit || pkt_discard) && !pend) begin pend <= 1; pend_commit <= pkt_commit && !pkt_discard; end
     if (pend && drained) begin
      pend <= 0;
      s_commit <= pend_commit;
      if (pend_commit) begin
       r_seq <= r_seq + 1'b1; r_n <= w_n; r_below <= w_below; r_above <= w_above; r_hout <= w_hout;
       r_m <= w_n - w_below - w_above;
       r_zmin <= w_zmin; r_zmax <= w_zmax; r_zsum <= w_zsum; r_xmin <= w_xmin; r_xmax <= w_xmax;
       r_ymin <= w_ymin; r_ymax <= w_ymax; r_hmin <= w_hmin; r_hmax <= w_hmax;
       s_target <= (w_n - w_below - w_above - 1'b1) >> 1;
       r_median_valid <= 0; r_median <= 0;
       wb <= ~wb;                      // finished bank becomes the result bank
      end
      s_idx <= 0; s_cum <= 0; s_found <= 0; s_state <= S_SCAN;
     end
    end
    S_SCAN: begin
     // cycle k+1 sees bin k (read in cycle k): median search on committed packets; the bin
     // is cleared in the same cycle (write port, one address behind the read)
     if (s_idx != 0) begin
      if (s_commit && !s_found && s_cum + h_do > s_target && r_m != 0) begin
       r_median <= s_idx[7:0] - 1'b1; r_median_valid <= 1; s_found <= 1;
      end
      s_cum <= s_cum + h_do;
     end
     if (s_idx == 256) begin
      s_state <= S_RUN;
      w_n <= 0; w_below <= 0; w_above <= 0; w_hout <= 0; w_zsum <= 0;
      w_zmin <= 32'sh7fffffff; w_zmax <= 32'sh80000000; w_xmin <= 32'sh7fffffff; w_xmax <= 32'sh80000000;
      w_ymin <= 32'sh7fffffff; w_ymax <= 32'sh80000000; w_hmin <= 32'sh7fffffff; w_hmax <= 32'sh80000000;
     end
     s_idx <= s_idx + 1'b1;
    end
    default: begin                     // S_CLEAR after reset: empty both banks and the histogram
     s_idx <= s_idx + 1'b1;
     if (s_idx == 255) s_state <= S_RUN;
    end
   endcase
  end
 end

 always @(*) begin
  case (reg_raddr)
   8'h00: reg_rdata = MAGIC;
   8'h04: reg_rdata = {30'd0, use_preview, enable};
   8'h08: reg_rdata = z_origin;
   8'h0C: reg_rdata = {27'd0, z_shift};
   8'h10: reg_rdata = h_origin;
   8'h14: reg_rdata = {27'd0, h_shift};
   8'h18: reg_rdata = {27'd0, band_sel};
   8'h20: reg_rdata = r_seq;
   8'h24: reg_rdata = {16'd0, r_n};
   8'h28: reg_rdata = r_zmin;
   8'h2C: reg_rdata = r_zmax;
   8'h30: reg_rdata = r_zsum[31:0];
   8'h34: reg_rdata = {{16{r_zsum[47]}}, r_zsum[47:32]};
   8'h38: reg_rdata = {r_median_valid, 23'd0, r_median};
   8'h3C: reg_rdata = {16'd0, r_m};
   8'h40: reg_rdata = {16'd0, r_below};
   8'h44: reg_rdata = {16'd0, r_above};
   8'h48: reg_rdata = r_xmin;
   8'h4C: reg_rdata = r_xmax;
   8'h50: reg_rdata = r_ymin;
   8'h54: reg_rdata = r_ymax;
   8'h58: reg_rdata = r_hmin;
   8'h5C: reg_rdata = r_hmax;
   8'h60: reg_rdata = dropped;
   8'h64: reg_rdata = {16'd0, b_out[207:192]};
   8'h68: reg_rdata = b_out[159:128];
   8'h6C: reg_rdata = b_out[191:160];
   8'h70: reg_rdata = b_out[95:64];
   8'h74: reg_rdata = b_out[127:96];
   8'h78: reg_rdata = b_out[31:0];
   8'h7C: reg_rdata = b_out[63:32];
   8'h80: reg_rdata = {16'd0, r_hout};
   default: reg_rdata = 32'd0;
  endcase
 end
endmodule
`default_nettype wire
