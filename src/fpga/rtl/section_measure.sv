`timescale 1ns/1ps
`default_nettype none
// Section measurement of the laser profile, per committed PFO3 packet (model/section_measure_model.py).
// Read-only snoop of geo_out_record, same commit rule as r41_measure.
//
// Collect: window points projected into the laser plane, s = (p-o).e_s, h = (p-o).e_h (Q16 mm),
//          stored in a 1024 x 64 point memory.
// Solve:   insertion sort by s, medians of spacing and noise, median-3 segmentation, a refitted
//          least-squares line per segment, median offsets, main step, step table, gap.
// Medians are exact: each order statistic is found bit by bit, one pass over the data per bit.
// The segment table has one read port (index ti); the reference and second segments are kept
// in registers. Points that arrive while solving, or beyond 1024, are dropped and counted.
// Registers (offset in the 0x700 window): docs/section_measure_design.md.
module section_measure(
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
 localparam [31:0] MAGIC     = 32'h5345434D;
 localparam [31:0] I32_MAX   = 32'h7fffffff;
 localparam [31:0] RMS_FLOOR = 32'd1311;
 localparam [31:0] NOISE_K   = 32'd39667;

 // saturate to 32 bits: the value fits when every bit above bit 31 equals the sign
 function automatic signed [31:0] sat32(input signed [111:0] v);
  if (&v[111:31] || ~|v[111:31]) sat32 = v[31:0];
  else sat32 = v[111] ? 32'h80000000 : I32_MAX;
 endfunction
 function automatic signed [27:0] sat28(input signed [32:0] v);
  if (&v[32:27] || ~|v[32:27]) sat28 = v[27:0];
  else sat28 = v[32] ? 28'h8000000 : 28'h7ffffff;
 endfunction
 function automatic [32:0] mag(input signed [31:0] v);      // |v| without overflow
  mag = v[31] ? -$signed({v[31], v}) : {1'b0, v};
 endfunction
 function automatic signed [31:0] smax(input signed [31:0] a, input signed [31:0] b);
  smax = (a > b) ? a : b;
 endfunction

 // ---------------- configuration
 reg        enable, use_preview;
 reg signed [31:0] org_x, org_y, org_z;
 reg signed [24:0] es_x, es_y, es_z, eh_x, eh_y, eh_z;
 reg signed [31:0] z_min, z_max, cfg_jump, cfg_brk, cfg_level;
 reg [10:0] min_points;
 reg [3:0]  step_sel, seg_sel;

 // ---------------- point memory: write port A, read port B (registered address and data)
 (* ram_style = "block" *) reg [63:0] pmem [0:1023];
 reg        pwe;
 reg  [9:0] pwaddr, praddr;
 reg [63:0] pwdata;
 (* dont_touch = "yes" *) reg [63:0] pq;
 always @(posedge clk) begin
  if (pwe) pmem[pwaddr] <= pwdata;
  pq <= pmem[praddr];
 end
 wire signed [31:0] q_s = pq[63:32];
 wire signed [31:0] q_h = pq[31:0];

 // ---------------- states
 localparam [6:0] S_SEG_FLUSH = 64, S_GAP_DEC = 65, S_GAP_W = 66,
                  S_REF_D = 67, S_REF2_D = 68, S_MED_DONE = 69,
                  S_THRESH_NZ = 70, S_THRESH_PRE = 71;
 localparam [6:0]
  S_COLLECT  = 0,  S_SORT_RD  = 1,  S_SORT_KEY = 2,  S_SORT_CMP = 3,  S_SORT_PUT = 4,
  S_SPACING  = 5,  S_NOISE    = 6,  S_THRESH0  = 7,  S_THRESH1  = 8,  S_SEG      = 9,
  S_SEG_END  = 10, S_SEG_LAST = 11, S_FIT_RD   = 12, S_FIT_GET  = 13, S_FIT_MOM  = 14,
  S_L0       = 15, S_L1       = 16, S_L2       = 17, S_L3       = 18, S_L4       = 19,
  S_L4B      = 20, S_L5       = 21, S_L6       = 22, S_L6B      = 23, S_L7       = 24,
  S_L8       = 25, S_L9       = 26, S_L9B      = 27, S_L10      = 28, S_KEEP     = 29,
  S_KEEP_W   = 30, S_INV1     = 31, S_INV2     = 32, S_INV3     = 33, S_FIT_WR   = 34,
  S_ADJ      = 35, S_ADJ_L    = 36, S_ADJ_G    = 37, S_ADJ_M    = 38, S_ADJ_W    = 39,
  S_REF      = 40, S_REF_C    = 41, S_REF2     = 42, S_REF2_C   = 43, S_REFOFF   = 44,
  S_REFOFF_G = 45, S_REFOFF_M = 46, S_REFOFF_W = 47, S_STEP     = 48, S_STEP_M   = 49,
  S_STEP_W   = 50, S_GAP      = 51, S_GAP_C    = 52, S_GAP_S0   = 53, S_GAP_S1   = 54,
  S_GAP_S2   = 55, S_GAP_S3   = 56, S_DEPTH    = 57, S_DEPTH_W  = 58, S_PUBLISH  = 59,
  S_PUB_W    = 60, S_PUB_END  = 61, S_MEDIAN   = 62, S_MED_PASS = 63;
 reg [6:0]  state, med_ret;
 reg        pend, sort_wait;

 // ---------------- collection pipeline
 wire [31:0] flags = geo_record[255:224];
 wire signed [31:0] geo_z = geo_record[127:96];
 wire take = geo_fire && enable && flags[8] && (use_preview ? flags[1] : flags[0]);
 wire collecting = state == S_COLLECT && !pend;
 reg  c0_v, c1_v, c2_v, c3_v, c4_v;
 reg signed [31:0] c0_x, c0_y, c0_z;
 reg signed [32:0] c1_x, c1_y, c1_z;
 (* use_dsp = "yes" *) reg signed [57:0] c2_sx, c2_sy, c2_sz, c2_hx, c2_hy, c2_hz;
 (* use_dsp = "yes" *) reg signed [58:0] c3_sxy, c3_hxy;
 reg signed [57:0] c3_sz, c3_hz;
 (* use_dsp = "yes" *) reg signed [59:0] c4_s, c4_h;
 reg [10:0] n_pts;
 reg [31:0] dropped;
 wire c_empty = !c0_v && !c1_v && !c2_v && !c3_v && !c4_v;

 // ---------------- scan pipeline: one point per cycle over [scan_lo, scan_hi)
 localparam [2:0] M_DS = 0, M_D2 = 1, M_RES = 2, M_MIN = 3, M_MOM = 4, M_KEEP = 5, M_SEG = 6;
 reg [2:0]  scan_mode;
 reg        scan_run;
 reg [10:0] scan_ptr, scan_lo, scan_hi;
 reg signed [31:0] scan_a, scan_c, scan_s0, mom_s0, mom_h0;
 reg [33:0] keep_lim;
 reg        sa_v, sb_v, sc_v, sd_v, se_v;
 reg        sg1_v, sg2_v;
 reg [10:0] sa_i;
 reg signed [31:0] sb_s, sb_h, sc_h, s_prev1, h_prev1;
 (* use_dsp = "yes" *) reg signed [34:0] h_prev_delta, sb_d2;
 reg [34:0] sc_d2;
 (* use_dsp = "yes" *) reg signed [32:0] sb_ds;
 (* use_dsp = "yes" *) reg signed [65:0] sd_base;
 reg signed [32:0] sb_dsr;
 reg signed [27:0] sb_dsm, sb_dhm, sc_dsm, sc_dhm, sd_dsm, sd_dhm, se_dsm, se_dhm;
 reg signed [31:0] sc_val, sd_val, se_val, se_r;
 reg        sb_emit, sc_emit, sd_emit, se_emit;
 (* use_dsp = "yes" *) reg signed [64:0] sc_prod;
 reg signed [64:0] sd_prod;
 (* use_dsp = "yes" *) reg signed [55:0] sc_xx, sc_xy, sc_yy;
 reg signed [55:0] sd_xx, sd_xy, sd_yy, se_xx, se_xy, se_yy;
 wire scan_busy = scan_run || sa_v || sb_v || sc_v || sd_v || se_v || sg1_v || sg2_v;

 // consumers
 reg [31:0] t_lo, t_hi;                       // bisection thresholds, offset binary
 reg [10:0] cnt_lo, cnt_hi;
 reg signed [31:0] r_min;
 reg [10:0] acc_n;
 (* use_dsp = "yes" *) reg signed [38:0] acc_sx, acc_sy;
 (* use_dsp = "yes" *) reg signed [65:0] acc_sxx, acc_sxy, acc_syy;
 // compare key registered with se_r/se_val: keeps the scan_mode mux off the cnt_lo/cnt_hi path
 reg  [31:0] se_u;

 // ---------------- arithmetic units
 reg         mul_go, div_go, sqrt_go, op_wait;
 reg  signed [39:0] mul_a;
 reg  signed [67:0] mul_b;
 wire signed [107:0] mul_p;
 wire        mul_done, div_done, sqrt_done;
 reg  [103:0] div_n;
 reg  [76:0]  div_d;
 wire [63:0]  div_q;
 reg  [63:0]  sqrt_v;
 wire [31:0]  sqrt_r;
 sm_mul  mul (.clk(clk), .go(mul_go), .a(mul_a), .b(mul_b), .p(mul_p), .done(mul_done));
 sm_div  div (.clk(clk), .rst(rst), .go(div_go), .num(div_n), .den(div_d), .q(div_q), .done(div_done));
 sm_sqrt sqr (.clk(clk), .rst(rst), .go(sqrt_go), .v(sqrt_v), .r(sqrt_r), .done(sqrt_done));
 wire ready = !op_wait && !mul_go && !div_go && !sqrt_go;
 // The multiplier publishes p with done.  op_wait clears on the next
 // edge, so result-consuming states cannot run until this registered
 // saturation is valid.  Use that existing handshake cycle to separate
 // the wide sign reduction from absolute-value/threshold decisions.
 reg signed [31:0] mul_q24;
 always @(posedge clk) mul_q24 <= sat32(mul_p >>> 24);
 reg [31:0] div_q31;
 always @(posedge clk) div_q31 <= (|div_q[63:31]) ? I32_MAX : div_q[31:0];

 // ---------------- segment table: LUT memories, one read port (ti)
 reg [3:0]  ti;
 reg [10:0] t_i0 [0:15], t_i1 [0:15];
 reg signed [31:0] t_s0 [0:15], t_h0 [0:15], t_s1 [0:15], t_h1 [0:15];
 reg signed [31:0] t_a [0:15], t_c [0:15], t_rms [0:15], t_off [0:15], st_at [0:15], st_off [0:15];
 reg [24:0] t_inv [0:15];
 wire [10:0] rd_i0 = t_i0[ti], rd_i1 = t_i1[ti];
 wire signed [31:0] rd_s0 = t_s0[ti], rd_h0 = t_h0[ti], rd_s1 = t_s1[ti], rd_h1 = t_h1[ti];
 wire signed [31:0] rd_a = t_a[ti], rd_c = t_c[ti], rd_rms = t_rms[ti], rd_off = t_off[ti];
 wire signed [31:0] rd_at = st_at[ti], rd_soff = st_off[ti];
 wire [24:0] rd_inv = t_inv[ti];
 wire [10:0] rd_n = rd_i1 - rd_i0;
 wire signed [32:0] rd_len = rd_s1 - rd_s0;
 reg [4:0]  n_seg;
 reg        seg_over;
 reg [3:0]  n_steps;
 // published tables
 reg [10:0] rs_n [0:15];
 reg signed [31:0] rs_s0 [0:15], rs_s1 [0:15], rs_off [0:15], rs_a [0:15], rt_at [0:15], rt_off [0:15];

 // ---------------- solve registers
 reg [10:0] i_sort, j_sort;
 reg [63:0] key;
 reg [31:0] spacing, noise, med_res;
 (* use_dsp = "yes" *) reg [47:0] noise_product;
 reg [33:0] thr_hole;
 reg [34:0] thr_jump, thr_level;
 reg signed [31:0] hole, jump, level;
 reg [4:0]  k;                                // segment loop index, reaches 16
 reg [4:0]  pub_i;
 reg        sec_ok, prev_ok, best_ok, step_ok, depth_ok, gap_ok, refit, use_div;
 reg signed [77:0] t_d;
 reg signed [107:0] t_num, t6;
 reg signed [31:0] f_a, f_b, f_c, f_rms;
 reg [10:0] f_n;
 reg signed [38:0] f_sx, f_sy;
 reg signed [65:0] f_sxx, f_sxy, f_syy;
 reg [10:0] g_i0r, g_i1r;
 reg signed [31:0] g_s0r, g_h0r;
 // line L and segment g of the current offset (adjacent pairs)
 reg signed [31:0] l_a, l_c, l_s0, l_s1;
 reg [24:0] l_inv;
 // reference and second segments
 reg [3:0]  ref_k, sec_k;
 reg [10:0] ref_n, sec_n, ref_i0, ref_i1, sec_i0, sec_i1;
 reg signed [32:0] ref_len, sec_len;
 reg [10:0] ref_cmp_n;
 (* use_dsp = "yes" *) reg signed [32:0] ref_cmp_len;
 reg signed [31:0] ref_s0, ref_a, ref_c, ref_rms, sec_s0, sec_a, sec_c, sec_rms;
 reg [24:0] ref_inv, sec_inv;
 // gap search
 reg signed [31:0] prev_s1, prev_h1, a_s1, a_h1, b_s0, b_h0;
 reg [10:0] prev_i1, a_i1, b_i0;
 reg signed [32:0] best_d;
 reg signed [32:0] gap_d;
 reg [32:0] gap_off;
 reg gap_level, gap_better;
 // outputs of this packet
 reg signed [31:0] o_step, o_gap, o_depth, o_e0, o_e1, o_rms, o_a;
 reg [3:0]  o_ref, o_stepseg;
 reg [31:0] solve_cycles;
 // segmentation
 reg [10:0] g_cnt, g_i0;
 reg signed [31:0] g_sj, g_hj, g_sjm, g_hjm, g_s0, g_h0, g_mprev;
 // Median, difference and decision retain a common sample identity.  History
 // advances at the input stage; segment origins advance in decision order.
 reg [10:0] sg1_cnt, sg2_cnt;
 reg signed [31:0] sg1_m, sg1_prev, sg1_sj, sg1_hj, sg1_sjm, sg1_hjm;
 reg signed [31:0] sg2_sj, sg2_hj, sg2_sjm, sg2_hjm;
 (* use_dsp = "yes" *) reg signed [32:0] sg1_ds, sg2_ds, sg2_dm;
 // bisection
 reg [5:0]  med_bit;
 reg [10:0] med_count, k_lo, k_hi;
 reg [31:0] p_lo, p_hi;

 // ---------------- results (last committed packet)
 reg [31:0] r_packets, r_cycles;
 reg [10:0] r_points;
 reg [4:0]  r_nseg;
 reg        r_over, r_step_ok, r_gap_ok, r_depth_ok, r_has_seg;
 reg [3:0]  r_ref, r_stepseg, r_nsteps;
 reg signed [31:0] r_step, r_gap, r_depth, r_noise, r_level, r_rms, r_slope, r_spacing, r_e0, r_e1;

 // ---------------- segmentation helpers (stage B in index order)
 wire signed [31:0] med3 = (g_hjm > g_hj) ? ((g_hj > sb_h) ? g_hj : ((g_hjm > sb_h) ? sb_h : g_hjm))
                                          : ((g_hjm > sb_h) ? g_hjm : ((g_hj > sb_h) ? sb_h : g_hj));
 wire signed [31:0] m_j = (g_cnt == 1) ? g_hj : med3;
 wire signed [32:0] ds_j = g_sj - g_sjm;
 wire brk_j    = sg2_cnt >= 2 && (sg2_ds > hole || sg2_dm > jump || -sg2_dm > jump);
 // (n, length) of the row at ti against the reference / second
 wire gt_ref = ref_cmp_n > ref_n || (ref_cmp_n == ref_n && ref_cmp_len > ref_len);
 wire gt_sec = ref_cmp_n > sec_n || (ref_cmp_n == sec_n && ref_cmp_len > sec_len);

 // ---------------- main sequential block
 always @(posedge clk) begin : main
  reg [1:0] drop_inc;
  reg seg_we;
  reg [10:0] seg_i0, seg_i1;
  reg signed [31:0] seg_s1, seg_h1;
  pwe <= 1'b0;
  mul_go <= 1'b0;
  div_go <= 1'b0;
  sqrt_go <= 1'b0;
  seg_we = 1'b0;
  seg_i0 = 0; seg_i1 = 0; seg_s1 = 0; seg_h1 = 0;
  if (mul_done || div_done || sqrt_done) op_wait <= 1'b0;
  if (rst) begin
   enable <= 1'b0; use_preview <= 1'b1;
   org_x <= 0; org_y <= 0; org_z <= 0;
   es_x <= 0; es_y <= 0; es_z <= 0; eh_x <= 0; eh_y <= 0; eh_z <= 0;
   z_min <= 32'h80000000; z_max <= I32_MAX;
   cfg_jump <= 32'd52429; cfg_brk <= 32'd65536; cfg_level <= 32'd32768;
   min_points <= 11'd6; step_sel <= 0; seg_sel <= 0;
   state <= S_COLLECT; pend <= 1'b0; sort_wait <= 1'b0; n_pts <= 0; dropped <= 0;
   c0_v <= 0; c1_v <= 0; c2_v <= 0; c3_v <= 0; c4_v <= 0;
   scan_run <= 0; sa_v <= 0; sb_v <= 0; sc_v <= 0; sd_v <= 0; se_v <= 0;
   sg1_v <= 0; sg2_v <= 0;
   op_wait <= 0; pub_i <= 0; solve_cycles <= 0; ti <= 0;
   r_packets <= 0; r_cycles <= 0; r_points <= 0; r_nseg <= 0; r_over <= 0;
   r_step_ok <= 0; r_gap_ok <= 0; r_depth_ok <= 0; r_has_seg <= 0;
   r_ref <= 0; r_stepseg <= 0; r_nsteps <= 0;
   r_step <= 0; r_gap <= 0; r_depth <= 0; r_noise <= 0; r_level <= 0;
   r_rms <= 0; r_slope <= 0; r_spacing <= 0; r_e0 <= 0; r_e1 <= 0;
  end else begin
   // ---- register writes
   if (reg_we) case (reg_waddr)
    8'h04: begin enable <= reg_wdata[0]; use_preview <= reg_wdata[1]; end
    8'h08: org_x <= reg_wdata;
    8'h0C: org_y <= reg_wdata;
    8'h10: org_z <= reg_wdata;
    8'h14: es_x <= reg_wdata[24:0];
    8'h18: es_y <= reg_wdata[24:0];
    8'h1C: es_z <= reg_wdata[24:0];
    8'h20: eh_x <= reg_wdata[24:0];
    8'h24: eh_y <= reg_wdata[24:0];
    8'h28: eh_z <= reg_wdata[24:0];
    8'h2C: z_min <= reg_wdata;
    8'h30: z_max <= reg_wdata;
    8'h34: cfg_jump <= reg_wdata;
    8'h38: cfg_brk <= reg_wdata;
    8'h3C: cfg_level <= reg_wdata;
    8'h40: min_points <= reg_wdata[10:0];
    8'h44: begin step_sel <= reg_wdata[3:0]; seg_sel <= reg_wdata[11:8]; end
    default: ;
   endcase

   // ---- collection: window test, q = p - o, products, sums, store
   drop_inc = 0;
   c0_v <= take && collecting && geo_z >= z_min && geo_z <= z_max;
   if (take && !collecting) drop_inc = drop_inc + 1;
   c0_x <= geo_record[63:32];
   c0_y <= geo_record[95:64];
   c0_z <= geo_z;
   c1_v <= c0_v;
   c1_x <= c0_x - org_x;
   c1_y <= c0_y - org_y;
   c1_z <= c0_z - org_z;
   c2_v <= c1_v;
   c2_sx <= c1_x * es_x; c2_sy <= c1_y * es_y; c2_sz <= c1_z * es_z;
   c2_hx <= c1_x * eh_x; c2_hy <= c1_y * eh_y; c2_hz <= c1_z * eh_z;
   c3_v <= c2_v;
   c3_sxy <= c2_sx + c2_sy; c3_sz <= c2_sz;
   c3_hxy <= c2_hx + c2_hy; c3_hz <= c2_hz;
   c4_v <= c3_v;
   c4_s <= c3_sxy + c3_sz;
   c4_h <= c3_hxy + c3_hz;
   if (c4_v) begin
    if (n_pts < 11'd1024) begin
     pwe <= 1'b1;
     pwaddr <= n_pts[9:0];
     pwdata <= {sat32(c4_s >>> 24), sat32(c4_h >>> 24)};
     n_pts <= n_pts + 1;
    end else begin
     drop_inc = drop_inc + 1;
    end
   end
   dropped <= dropped + drop_inc;
   if (pkt_discard && state == S_COLLECT) begin
    c0_v <= 0; c1_v <= 0; c2_v <= 0; c3_v <= 0; c4_v <= 0;
    pwe <= 1'b0;
    n_pts <= 0;
    pend <= 1'b0;
   end else if (pkt_commit && state == S_COLLECT && enable) begin
    pend <= 1'b1;
   end

   // ---- scan pipeline (A: issue, B: data, C-D: products, E: residual, F: consume)
   sa_v <= 1'b0;
   if (scan_run) begin
    if (scan_ptr < scan_hi) begin
     sa_v <= 1'b1;
     sa_i <= scan_ptr;
     scan_ptr <= scan_ptr + 1;
     praddr <= scan_ptr[9:0] + 1;
    end else begin
     scan_run <= 1'b0;
    end
   end
   sb_v <= sa_v;
   sb_emit <= 1'b0;
   if (sa_v) begin : stage_b
    sb_s <= q_s;
    sb_h <= q_h;
    sb_dsr <= q_s - scan_s0;
    sb_dsm <= sat28(q_s - mom_s0);
    sb_dhm <= sat28(q_h - mom_h0);
    s_prev1 <= q_s;
    h_prev1 <= q_h;
    // The history delta belongs to the previous point: h[j-2]-2*h[j-1].
    // Prepare the next one in parallel, so BRAM data crosses just one
    // adder before the register, rather than subtract/add/abs/saturate.
    h_prev_delta <= $signed(h_prev1) - 2 * $signed(q_h);
    sb_d2 <= $signed(q_h) + h_prev_delta;
    sb_ds <= $signed(q_s) - $signed(s_prev1);
    case (scan_mode)
     M_DS: begin
      sb_emit <= sa_i >= scan_lo + 1;
     end
     M_D2: begin
      sb_emit <= sa_i >= scan_lo + 2;
     end
     default: sb_emit <= 1'b1;
    endcase
   end
   sc_v <= sb_v;
   sc_prod <= scan_a * sb_dsr;
   sc_xx <= sb_dsm * sb_dsm;
   sc_xy <= sb_dsm * sb_dhm;
   sc_yy <= sb_dhm * sb_dhm;
   sc_d2 <= sb_d2[34] ? -sb_d2 : sb_d2;
   sc_h <= sb_h; sc_val <= sat32(sb_ds); sc_emit <= sb_emit; sc_dsm <= sb_dsm; sc_dhm <= sb_dhm;
   sd_v <= sc_v;
   sd_prod <= sc_prod;
   sd_xx <= sc_xx; sd_xy <= sc_xy; sd_yy <= sc_yy;
   sd_base <= $signed(sc_h) - $signed(scan_c);
   sd_val <= scan_mode == M_D2 ? ((|sc_d2[34:31]) ? I32_MAX : sc_d2[31:0]) : sc_val;
   sd_emit <= sc_emit; sd_dsm <= sc_dsm; sd_dhm <= sc_dhm;
   se_v <= sd_v;
   se_r <= sat32(sd_base - (sd_prod >>> 24));
   se_xx <= sd_xx; se_xy <= sd_xy; se_yy <= sd_yy;
   se_val <= sd_val; se_emit <= sd_emit; se_dsm <= sd_dsm; se_dhm <= sd_dhm;
   se_u <= (scan_mode == M_RES ? sat32(sd_base - (sd_prod >>> 24)) : sd_val) ^ 32'h80000000;
   if (se_v) case (scan_mode)
    M_DS, M_D2, M_RES: if (se_emit) begin
     if (se_u <= t_lo) cnt_lo <= cnt_lo + 1;
     if (se_u <= t_hi) cnt_hi <= cnt_hi + 1;
    end
    M_MIN: if (se_r < r_min) r_min <= se_r;
    M_MOM, M_KEEP: if (scan_mode == M_MOM || {1'b0, mag(se_r)} <= keep_lim) begin
     acc_n <= acc_n + 1;
     acc_sx <= acc_sx + se_dsm;
     acc_sy <= acc_sy + se_dhm;
     acc_sxx <= acc_sxx + se_xx;
     acc_sxy <= acc_sxy + se_xy;
     acc_syy <= acc_syy + se_yy;
    end
    default: ;
   endcase

   // ---- segmentation: median, difference, then ordered segment decision.
   sg1_v <= sb_v && scan_mode == M_SEG && g_cnt != 0;
   sg2_v <= sg1_v;
   if (sb_v && scan_mode == M_SEG) begin
    if (g_cnt == 0) begin
     g_i0 <= 0;
     g_s0 <= sb_s;
     g_h0 <= sb_h;
    end else begin
     sg1_cnt <= g_cnt;
     sg1_m <= m_j; sg1_prev <= g_mprev;
     sg1_ds <= ds_j;
     sg1_sj <= g_sj; sg1_hj <= g_hj;
     sg1_sjm <= g_sjm; sg1_hjm <= g_hjm;
     g_mprev <= m_j;
    end
    g_sjm <= g_sj; g_hjm <= g_hj;
    g_sj <= sb_s; g_hj <= sb_h;
    g_cnt <= g_cnt + 1;
   end
   if (sg1_v) begin
    sg2_cnt <= sg1_cnt;
    sg2_dm <= sg1_m - sg1_prev;
    sg2_ds <= sg1_ds;
    sg2_sj <= sg1_sj; sg2_hj <= sg1_hj;
    sg2_sjm <= sg1_sjm; sg2_hjm <= sg1_hjm;
   end
   if (sg2_v && brk_j) begin
    if (sg2_cnt - 1 - g_i0 >= min_points) begin
     seg_we = 1'b1; seg_i0 = g_i0; seg_i1 = sg2_cnt - 1;
     seg_s1 = sg2_sjm; seg_h1 = sg2_hjm;
    end
    g_i0 <= sg2_cnt - 1;
    g_s0 <= sg2_sj; g_h0 <= sg2_hj;
   end

   // ---- solver
   solve_cycles <= (state == S_COLLECT) ? 32'd0 : solve_cycles + 1;
   case (state)
    S_COLLECT: if (pend && c_empty && !pwe) begin
     pend <= 1'b0;
     n_seg <= 0; seg_over <= 0; n_steps <= 0;
     step_ok <= 0; depth_ok <= 0; gap_ok <= 0;
     o_step <= 0; o_gap <= 0; o_depth <= 0; o_e0 <= 0; o_e1 <= 0; o_ref <= 0; o_stepseg <= 0;
     o_rms <= 0; o_a <= 0;
     i_sort <= 11'd1;
     state <= (n_pts > 1) ? S_SORT_RD : S_SPACING;
    end
    // ---- insertion sort (stable); every memory read takes two cycles
    S_SORT_RD: begin
     praddr <= i_sort[9:0];
     sort_wait <= 1'b1;
     state <= S_SORT_KEY;
    end
    S_SORT_KEY: if (sort_wait) sort_wait <= 1'b0; else begin
     key <= pq;
     j_sort <= i_sort - 1;
     praddr <= i_sort[9:0] - 1;
     sort_wait <= 1'b1;
     state <= S_SORT_CMP;
    end
    S_SORT_CMP: if (sort_wait) sort_wait <= 1'b0; else begin
     pwe <= 1'b1;
     if (q_s > $signed(key[63:32])) begin
      pwaddr <= j_sort[9:0] + 1;
      pwdata <= pq;
      if (j_sort == 0) state <= S_SORT_PUT;
      else begin
       j_sort <= j_sort - 1;
       praddr <= j_sort[9:0] - 1;
       sort_wait <= 1'b1;
      end
     end else begin
      pwaddr <= j_sort[9:0] + 1;
      pwdata <= key;
      sort_next();
     end
    end
    S_SORT_PUT: begin
     pwe <= 1'b1;
     pwaddr <= 0;
     pwdata <= key;
     sort_next();
    end
    // ---- spacing and noise medians, thresholds
    S_SPACING: begin
     scan_lo <= 0; scan_hi <= n_pts; scan_mode <= M_DS;
     start_median((n_pts > 1) ? n_pts - 1 : 11'd0, S_NOISE);
    end
    S_NOISE: begin
     spacing <= med_res;
     scan_lo <= 0; scan_hi <= n_pts; scan_mode <= M_D2;
     start_median((n_pts > 2) ? n_pts - 2 : 11'd0, S_THRESH0);
    end
    S_THRESH0: begin
     noise_product <= med_res * NOISE_K[15:0];
     state <= S_THRESH_NZ;
    end
    S_THRESH_NZ: begin
     noise <= noise_product[47:16];
     state <= S_THRESH_PRE;
    end
    S_THRESH_PRE: begin
     thr_hole <= {2'd0, spacing} << 2;
     thr_jump <= {3'd0, noise} * 3'd5;
     thr_level <= {3'd0, noise} * 2'd3;
     state <= S_THRESH1;
    end
    S_THRESH1: begin
     hole  <= smax(cfg_brk, sat32($signed({1'b0, thr_hole})));
     jump  <= smax(cfg_jump, sat32($signed({1'b0, thr_jump})));
     level <= smax(cfg_level, sat32($signed({1'b0, thr_level})));
     g_cnt <= 0;
     scan_mode <= M_SEG;
     start_scan(0, n_pts);
     state <= S_SEG;
    end
    S_SEG: if (!scan_busy) state <= S_SEG_END;
    // last element: m = h; then the final run
    S_SEG_END: begin
     // Flush the final median through the same registered decision path.
     sg2_v <= 1'b1; sg2_cnt <= g_cnt;
     sg2_dm <= g_hj - g_mprev; sg2_ds <= ds_j;
     sg2_sj <= g_sj; sg2_hj <= g_hj;
     sg2_sjm <= g_sjm; sg2_hjm <= g_hjm;
     state <= S_SEG_FLUSH;
    end
    S_SEG_FLUSH: state <= S_SEG_LAST;
    S_SEG_LAST: begin
     if (g_cnt >= 1 && g_cnt - g_i0 >= min_points) begin
      seg_we = 1'b1; seg_i0 = g_i0; seg_i1 = g_cnt; seg_s1 = g_sj; seg_h1 = g_hj;
     end
     k <= 0;
     state <= S_FIT_RD;
    end
    // ---- per segment: moments, line, keep within 3 rms, refit, 1/sqrt(1+a^2)
    S_FIT_RD: if (k >= n_seg) begin
     k <= 0;
     state <= (n_seg > 1) ? S_ADJ : S_REF;
    end else begin
     ti <= k[3:0];
     state <= S_FIT_GET;
    end
    S_FIT_GET: begin
     g_i0r <= rd_i0; g_i1r <= rd_i1; g_s0r <= rd_s0; g_h0r <= rd_h0;
     mom_s0 <= rd_s0;
     mom_h0 <= rd_h0;
     scan_mode <= M_MOM;
     start_scan(rd_i0, rd_i1);
     clear_acc();
     refit <= 1'b0;
     state <= S_FIT_MOM;
    end
    S_FIT_MOM: if (!scan_busy) begin
     load_acc();
     state <= S_L0;
    end
    S_L0: begin
     mul_a <= {29'd0, f_n}; mul_b <= f_sxx;
     go_mul(S_L1);
    end
    S_L1: if (ready) begin
     t_d <= mul_p[77:0];
     mul_a <= f_sx; mul_b <= f_sx;
     go_mul(S_L2);
    end
    S_L2: if (ready) begin
     t_d <= t_d - mul_p[77:0];
     mul_a <= {29'd0, f_n}; mul_b <= f_sxy;
     go_mul(S_L3);
    end
    S_L3: if (ready) begin
     t_num <= mul_p;
     mul_a <= f_sx; mul_b <= f_sy;
     go_mul(S_L4);
    end
    S_L4: if (ready) begin
     t_num <= t_num - mul_p;
     use_div <= t_d > 0;
     state <= S_L4B;
    end
    S_L4B: begin
     div_n <= (t_num < 0 ? -t_num : t_num) << 24;
     div_d <= t_d[76:0];
     if (use_div) go_div(S_L5);
     else state <= S_L5;
    end
    S_L5: if (ready) begin : l5
     reg signed [31:0] a;
     a = !use_div ? 32'sd0 : t_num < 0 ? -$signed(div_q31) : $signed(div_q31);
     f_a <= a;
     mul_a <= a; mul_b <= f_sx;
     go_mul(S_L6);
    end
    S_L6: if (ready) begin
     t_num <= f_sy - (mul_p >>> 24);
     state <= S_L6B;
    end
    S_L6B: begin
     div_n <= t_num < 0 ? -t_num : t_num;
     div_d <= {66'd0, f_n};
     go_div(S_L7);
    end
    S_L7: if (ready) begin : l7
     reg signed [31:0] b;
     b = t_num < 0 ? -$signed(div_q31) : $signed(div_q31);
     f_b <= b;
     mul_a <= f_a; mul_b <= f_sxy;
     go_mul(S_L8);
    end
    S_L8: if (ready) begin
     t6 <= f_syy - (mul_p >>> 24);
     mul_a <= f_b; mul_b <= f_sy;
     go_mul(S_L9);
    end
    S_L9: if (ready) begin
     t6 <= t6 - mul_p;
     state <= S_L9B;
    end
    S_L9B: begin
     div_n <= t6 < 0 ? 104'd0 : t6[103:0];
     div_d <= {66'd0, f_n};
     go_div(S_L10);
    end
    S_L10: if (ready) begin
     sqrt_v <= (div_q > 64'h4000000000000000) ? 64'h4000000000000000 : div_q;
     go_sqrt(S_KEEP);
    end
    S_KEEP: if (ready) begin : kp
     reg signed [31:0] c;
     c = sat32(g_h0r + f_b);
     f_rms <= sqrt_r;
     f_c <= c;
     if (!refit) begin
      keep_lim <= 3 * {2'b00, (sqrt_r > RMS_FLOOR ? sqrt_r : RMS_FLOOR)};
      scan_a <= f_a; scan_c <= c; scan_s0 <= g_s0r;
      scan_mode <= M_KEEP;
      start_scan(g_i0r, g_i1r);
      clear_acc();
      refit <= 1'b1;
      state <= S_KEEP_W;
     end else begin
      state <= S_INV1;
     end
    end
    S_KEEP_W: if (!scan_busy) begin
     if (acc_n >= 3 && acc_n < g_i1r - g_i0r) begin
      load_acc();
      state <= S_L0;
     end else begin
      state <= S_INV1;
     end
    end
    S_INV1: begin
     mul_a <= f_a; mul_b <= f_a;
     go_mul(S_INV2);
    end
    S_INV2: if (ready) begin
     sqrt_v <= 64'h0001000000000000 + mul_p[63:0];
     go_sqrt(S_INV3);
    end
    S_INV3: if (ready) begin
     div_n <= 104'h1000000000000;
     div_d <= {45'd0, sqrt_r};
     go_div(S_FIT_WR);
    end
    S_FIT_WR: if (ready) begin
     t_a[k[3:0]] <= f_a;
     t_c[k[3:0]] <= f_c;
     t_rms[k[3:0]] <= f_rms;
     t_inv[k[3:0]] <= div_q[24:0];
     k <= k + 1;
     state <= S_FIT_RD;
    end
    // ---- adjacent steps: segment k+1 against the line of k
    S_ADJ: if (k + 1 >= n_seg) begin
     k <= 0;
     state <= S_REF;
    end else begin
     ti <= k[3:0];
     state <= S_ADJ_L;
    end
    S_ADJ_L: begin
     l_a <= rd_a; l_c <= rd_c; l_s0 <= rd_s0; l_s1 <= rd_s1; l_inv <= rd_inv;
     ti <= k[3:0] + 1;
     state <= S_ADJ_G;
    end
    S_ADJ_G: start_res_median(rd_i0, rd_i1, l_a, l_c, l_s0, S_ADJ_M);
    S_ADJ_M: begin
     mul_a <= $signed(med_res); mul_b <= {43'd0, l_inv};
     go_mul(S_ADJ_W);
    end
    S_ADJ_W: if (ready) begin : adj
     reg signed [32:0] mid;
     mid = l_s1 + rd_s0;                      // ti still points at segment k+1
     if (mag(mul_q24) > level && n_steps < 4'd15) begin
      st_at[n_steps] <= mid >>> 1;
      st_off[n_steps] <= mul_q24;
      n_steps <= n_steps + 1;
     end
     k <= k + 1;
     state <= S_ADJ;
    end
    // ---- reference: most points, then longest; the second likewise
    S_REF: if (n_seg == 0) state <= S_PUBLISH;
     else if (k < n_seg) begin
      ti <= k[3:0];
      state <= S_REF_C;
     end else begin
      k <= 0;
      sec_ok <= 0;
      state <= S_REF2;
     end
    S_REF_C: begin
     ref_cmp_n <= rd_n;
     ref_cmp_len <= rd_len;
     state <= S_REF_D;
    end
    S_REF_D: begin
     if (k == 0 || gt_ref) begin
      ref_k <= k[3:0]; ref_n <= rd_n; ref_len <= rd_len; ref_i0 <= rd_i0; ref_i1 <= rd_i1;
      ref_s0 <= rd_s0; ref_a <= rd_a; ref_c <= rd_c; ref_rms <= rd_rms; ref_inv <= rd_inv;
     end
     k <= k + 1;
     state <= S_REF;
    end
    S_REF2: if (k < n_seg) begin
     ti <= k[3:0];
     state <= S_REF2_C;
    end else begin
     k <= 0;
     state <= S_REFOFF;
    end
    S_REF2_C: begin
     ref_cmp_n <= rd_n;
     ref_cmp_len <= rd_len;
     state <= S_REF2_D;
    end
    S_REF2_D: begin
     if (k[3:0] != ref_k && (!sec_ok || gt_sec)) begin
      sec_ok <= 1'b1;
      sec_k <= k[3:0]; sec_n <= rd_n; sec_len <= rd_len; sec_i0 <= rd_i0; sec_i1 <= rd_i1;
      sec_s0 <= rd_s0; sec_a <= rd_a; sec_c <= rd_c; sec_rms <= rd_rms; sec_inv <= rd_inv;
     end
     k <= k + 1;
     state <= S_REF2;
    end
    // ---- offsets of all segments against the reference line
    S_REFOFF: if (k >= n_seg) state <= S_STEP;
     else begin
      ti <= k[3:0];
      state <= S_REFOFF_G;
     end
    S_REFOFF_G: start_res_median(rd_i0, rd_i1, ref_a, ref_c, ref_s0, S_REFOFF_M);
    S_REFOFF_M: begin
     mul_a <= $signed(med_res); mul_b <= {43'd0, ref_inv};
     go_mul(S_REFOFF_W);
    end
    S_REFOFF_W: if (ready) begin
     t_off[k[3:0]] <= mul_q24;
     k <= k + 1;
     state <= S_REFOFF;
    end
    // ---- main step: right segment against the left one's line
    S_STEP: begin
     o_ref <= ref_k; o_rms <= ref_rms; o_a <= ref_a;
     k <= 0; prev_ok <= 0; best_ok <= 0;
     if (!sec_ok) state <= S_GAP;
     else if (sec_s0 < ref_s0) start_res_median(ref_i0, ref_i1, sec_a, sec_c, sec_s0, S_STEP_M);
     else start_res_median(sec_i0, sec_i1, ref_a, ref_c, ref_s0, S_STEP_M);
    end
    S_STEP_M: begin
     mul_a <= $signed(med_res); mul_b <= {43'd0, (sec_s0 < ref_s0) ? sec_inv : ref_inv};
     go_mul(S_STEP_W);
    end
    S_STEP_W: if (ready) begin
     if (mag(mul_q24) > level) begin
      step_ok <= 1'b1;
      o_step <= mul_q24;
      if (sec_s0 < ref_s0) begin
       o_ref <= sec_k; o_stepseg <= ref_k; o_rms <= sec_rms; o_a <= sec_a;
      end else begin
       o_stepseg <= sec_k;
      end
     end
     state <= S_GAP;
    end
    // ---- gap: largest hole between consecutive same-level segments (offsets against ref)
    S_GAP: if (k < n_seg) begin
     ti <= k[3:0];
     state <= S_GAP_C;
    end else begin
     state <= best_ok ? S_GAP_S0 : S_PUBLISH;
    end
    S_GAP_C: begin
     gap_d <= rd_s0 - prev_s1;
     gap_off <= mag(rd_off);
     state <= S_GAP_DEC;
    end
    S_GAP_DEC: begin
     gap_level <= gap_off <= level;
     gap_better <= prev_ok && gap_d > hole && (!best_ok || gap_d > best_d);
     state <= S_GAP_W;
    end
    S_GAP_W: begin
     if (gap_level) begin
       if (gap_better) begin
        best_ok <= 1'b1; best_d <= gap_d;
        a_s1 <= prev_s1; a_h1 <= prev_h1; a_i1 <= prev_i1;
        b_s0 <= rd_s0; b_h0 <= rd_h0; b_i0 <= rd_i0;
       end
      prev_s1 <= rd_s1; prev_h1 <= rd_h1; prev_i1 <= rd_i1;
      prev_ok <= 1'b1;
     end
     k <= k + 1;
     state <= S_GAP;
    end
    S_GAP_S0: begin
     mul_a <= sat32(b_s0 - a_s1);
     mul_b <= sat32(b_s0 - a_s1);
     go_mul(S_GAP_S1);
    end
    S_GAP_S1: if (ready) begin
     t6 <= mul_p;
     mul_a <= sat32(b_h0 - a_h1);
     mul_b <= sat32(b_h0 - a_h1);
     go_mul(S_GAP_S2);
    end
    S_GAP_S2: if (ready) begin
     sqrt_v <= t6[63:0] + mul_p[63:0];
     go_sqrt(S_GAP_S3);
    end
    S_GAP_S3: if (ready) begin
     gap_ok <= 1'b1;
     o_gap <= sqrt_r;
     o_e0 <= a_s1;
     o_e1 <= b_s0;
     if (a_i1 < b_i0) begin
      scan_a <= ref_a; scan_c <= ref_c; scan_s0 <= ref_s0;
      scan_mode <= M_MIN;
      start_scan(a_i1, b_i0);
      r_min <= I32_MAX;
      state <= S_DEPTH;
     end else begin
      state <= S_PUBLISH;
     end
    end
    S_DEPTH: if (!scan_busy) begin
     mul_a <= r_min; mul_b <= {43'd0, ref_inv};
     go_mul(S_DEPTH_W);
    end
    S_DEPTH_W: if (ready) begin
     o_depth <= sat32(-mul_q24);
     depth_ok <= 1'b1;
     state <= S_PUBLISH;
    end
    // ---- publish: tables one entry per cycle through ti, then the scalars
    S_PUBLISH: begin
     pub_i <= 0;
     ti <= 0;
     state <= S_PUB_W;
    end
    S_PUB_W: begin
     rs_n[pub_i[3:0]]   <= (pub_i < n_seg) ? rd_n : 11'd0;
     rs_s0[pub_i[3:0]]  <= rd_s0;
     rs_s1[pub_i[3:0]]  <= rd_s1;
     rs_off[pub_i[3:0]] <= rd_off;
     rs_a[pub_i[3:0]]   <= rd_a;
     rt_at[pub_i[3:0]]  <= rd_at;
     rt_off[pub_i[3:0]] <= rd_soff;
     pub_i <= pub_i + 1;
     ti <= pub_i[3:0] + 1;
     if (pub_i == 5'd15) state <= S_PUB_END;
    end
    S_PUB_END: begin
     r_packets <= r_packets + 1;
     r_cycles <= solve_cycles;
     r_points <= n_pts;
     r_nseg <= n_seg; r_over <= seg_over; r_has_seg <= n_seg != 0;
     r_step_ok <= step_ok; r_gap_ok <= gap_ok; r_depth_ok <= depth_ok;
     r_ref <= o_ref; r_stepseg <= o_stepseg; r_nsteps <= n_steps;
     r_step <= o_step; r_gap <= o_gap; r_depth <= o_depth;
     r_noise <= noise; r_level <= level; r_spacing <= spacing;
     r_rms <= o_rms; r_slope <= o_a;
     r_e0 <= o_e0; r_e1 <= o_e1;
     n_pts <= 0;
     state <= S_COLLECT;
    end
    // ---- exact median: two order statistics, one bit per pass, MSB first
    S_MEDIAN: if (med_count == 0) begin
     med_res <= 0;
     state <= med_ret;
    end else begin
     k_hi <= med_count >> 1;
     k_lo <= med_count[0] ? (med_count >> 1) : (med_count >> 1) - 1;
     p_lo <= 0;
     p_hi <= 0;
     med_bit <= 6'd31;
     start_pass(6'd31, 32'd0, 32'd0);
     state <= S_MED_PASS;
    end
    S_MED_PASS: if (!scan_busy) begin : pass
     reg [31:0] nl, nh;
     nl = (cnt_lo >= k_lo + 1) ? p_lo : (p_lo | (32'd1 << med_bit));
     nh = (cnt_hi >= k_hi + 1) ? p_hi : (p_hi | (32'd1 << med_bit));
     p_lo <= nl;
     p_hi <= nh;
     if (med_bit == 0) begin
      state <= S_MED_DONE;
     end else begin
      med_bit <= med_bit - 1;
      start_pass(med_bit - 1, nl, nh);
     end
    end
    S_MED_DONE: begin : median_done
     reg signed [32:0] sum;
     sum = $signed({~p_lo[31], ~p_lo[31], p_lo[30:0]}) + $signed({~p_hi[31], ~p_hi[31], p_hi[30:0]});
     med_res <= sum >>> 1;
     state <= med_ret;
    end
    default: state <= S_COLLECT;
   endcase

   // ---- segment table rows from segmentation (one write port)
   if (seg_we) begin
    if (n_seg < 16) begin
     t_i0[n_seg[3:0]] <= seg_i0;
     t_i1[n_seg[3:0]] <= seg_i1;
     t_s0[n_seg[3:0]] <= g_s0;
     t_h0[n_seg[3:0]] <= g_h0;
     t_s1[n_seg[3:0]] <= seg_s1;
     t_h1[n_seg[3:0]] <= seg_h1;
     n_seg <= n_seg + 1;
    end else begin
     seg_over <= 1'b1;
    end
   end
  end
 end

 // ---------------- solver tasks (called from the main block only)
 task automatic start_scan(input [10:0] lo, input [10:0] hi);
  begin
   scan_lo <= lo;
   scan_hi <= hi;
   scan_ptr <= lo;
   praddr <= lo[9:0];
   scan_run <= lo < hi;
  end
 endtask

 task automatic start_pass(input [5:0] b, input [31:0] plo, input [31:0] phi);
  begin
   t_lo <= plo | ((32'd1 << b) - 1);
   t_hi <= phi | ((32'd1 << b) - 1);
   cnt_lo <= 0;
   cnt_hi <= 0;
   start_scan(scan_lo, scan_hi);
  end
 endtask

 task automatic start_median(input [10:0] count, input [6:0] ret);
  begin
   med_count <= count;
   med_ret <= ret;
   state <= S_MEDIAN;
  end
 endtask

 // median of the residuals of points [lo, hi) against the line h = c + a (s - s0)
 task automatic start_res_median(input [10:0] lo, input [10:0] hi, input signed [31:0] a,
                                 input signed [31:0] c, input signed [31:0] s0, input [6:0] ret);
  begin
   scan_lo <= lo;
   scan_hi <= hi;
   scan_a <= a;
   scan_c <= c;
   scan_s0 <= s0;
   scan_mode <= M_RES;
   start_median(hi - lo, ret);
  end
 endtask

 task automatic sort_next;
  begin
   if (i_sort + 1 >= n_pts) state <= S_SPACING;
   else begin
    i_sort <= i_sort + 1;
    praddr <= i_sort[9:0] + 1;
    sort_wait <= 1'b1;
    state <= S_SORT_KEY;
   end
  end
 endtask

 task automatic clear_acc;
  begin
   acc_n <= 0; acc_sx <= 0; acc_sy <= 0; acc_sxx <= 0; acc_sxy <= 0; acc_syy <= 0;
  end
 endtask
 task automatic load_acc;
  begin
   f_n <= acc_n; f_sx <= acc_sx; f_sy <= acc_sy; f_sxx <= acc_sxx; f_sxy <= acc_sxy; f_syy <= acc_syy;
  end
 endtask

 task automatic go_mul(input [6:0] next);
  begin mul_go <= 1'b1; op_wait <= 1'b1; state <= next; end
 endtask
 task automatic go_div(input [6:0] next);
  begin div_go <= 1'b1; op_wait <= 1'b1; state <= next; end
 endtask
 task automatic go_sqrt(input [6:0] next);
  begin sqrt_go <= 1'b1; op_wait <= 1'b1; state <= next; end
 endtask

 // ---------------- register read
 always @(*) begin
  case (reg_raddr)
   8'h00: reg_rdata = MAGIC;
   8'h04: reg_rdata = {30'd0, use_preview, enable};
   8'h08: reg_rdata = org_x;
   8'h0C: reg_rdata = org_y;
   8'h10: reg_rdata = org_z;
   8'h14: reg_rdata = {{7{es_x[24]}}, es_x};
   8'h18: reg_rdata = {{7{es_y[24]}}, es_y};
   8'h1C: reg_rdata = {{7{es_z[24]}}, es_z};
   8'h20: reg_rdata = {{7{eh_x[24]}}, eh_x};
   8'h24: reg_rdata = {{7{eh_y[24]}}, eh_y};
   8'h28: reg_rdata = {{7{eh_z[24]}}, eh_z};
   8'h2C: reg_rdata = z_min;
   8'h30: reg_rdata = z_max;
   8'h34: reg_rdata = cfg_jump;
   8'h38: reg_rdata = cfg_brk;
   8'h3C: reg_rdata = cfg_level;
   8'h40: reg_rdata = {21'd0, min_points};
   8'h44: reg_rdata = {20'd0, seg_sel, 4'd0, step_sel};
   8'h50: reg_rdata = r_packets;
   8'h54: reg_rdata = {21'd0, r_points};
   8'h58: reg_rdata = {r_over, 26'd0, r_nseg};
   8'h5C: reg_rdata = {12'd0, r_nsteps, r_stepseg, r_ref, 4'd0, r_has_seg, r_depth_ok, r_gap_ok, r_step_ok};
   8'h60: reg_rdata = r_step;
   8'h64: reg_rdata = r_gap;
   8'h68: reg_rdata = r_depth;
   8'h6C: reg_rdata = r_noise;
   8'h70: reg_rdata = r_level;
   8'h74: reg_rdata = r_rms;
   8'h78: reg_rdata = r_slope;
   8'h7C: reg_rdata = r_spacing;
   8'h80: reg_rdata = rt_at[step_sel];
   8'h84: reg_rdata = rt_off[step_sel];
   8'h88: reg_rdata = {21'd0, rs_n[seg_sel]};
   8'h8C: reg_rdata = rs_s0[seg_sel];
   8'h90: reg_rdata = rs_s1[seg_sel];
   8'h94: reg_rdata = rs_off[seg_sel];
   8'h98: reg_rdata = rs_a[seg_sel];
   8'h9C: reg_rdata = dropped;
   8'hA0: reg_rdata = r_e0;
   8'hA4: reg_rdata = r_e1;
   8'hA8: reg_rdata = r_cycles;
   default: reg_rdata = 32'd0;
  endcase
 end
endmodule

// Pipelined signed multiplier on DSP slices; `done` marks the cycle after p is valid.
module sm_mul(
 input  wire clk, input wire go,
 input  wire signed [39:0] a, input wire signed [67:0] b,
 output reg  signed [107:0] p, output reg done
);
 reg signed [39:0] ar;
 reg signed [67:0] br;
 // Eight native DSP48 products (24x18 or 17x18), instead of a wide
 // inferred multiplier with LUT-generated partial products.  Low limbs
 // are zero-extended and the most significant limbs retain their sign.
 (* use_dsp = "yes" *) reg signed [41:0] pp00, pp01, pp02, pp03;
 (* use_dsp = "yes" *) reg signed [34:0] pp10, pp11, pp12, pp13;
 (* use_dsp = "yes" *) reg signed [58:0] lo01, lo23;
 (* use_dsp = "yes" *) reg signed [51:0] hi01, hi23;
 (* use_dsp = "yes" *) reg signed [91:0] lo_all;
 (* use_dsp = "yes" *) reg signed [84:0] hi_all;
 (* use_dsp = "yes" *) reg signed [107:0] product;
 reg [4:0] v;
 always @(posedge clk) begin
  ar <= a;
  br <= b;
  pp00 <= $signed({1'b0, ar[22:0]}) * $signed({1'b0, br[16:0]});
  pp01 <= $signed({1'b0, ar[22:0]}) * $signed({1'b0, br[33:17]});
  pp02 <= $signed({1'b0, ar[22:0]}) * $signed({1'b0, br[50:34]});
  pp03 <= $signed({1'b0, ar[22:0]}) * $signed(br[67:51]);
  pp10 <= $signed(ar[39:23]) * $signed({1'b0, br[16:0]});
  pp11 <= $signed(ar[39:23]) * $signed({1'b0, br[33:17]});
  pp12 <= $signed(ar[39:23]) * $signed({1'b0, br[50:34]});
  pp13 <= $signed(ar[39:23]) * $signed(br[67:51]);
  lo01 <= $signed(pp00) + ($signed({{17{pp01[41]}}, pp01}) <<< 17);
  lo23 <= $signed(pp02) + ($signed({{17{pp03[41]}}, pp03}) <<< 17);
  hi01 <= $signed(pp10) + ($signed({{17{pp11[34]}}, pp11}) <<< 17);
  hi23 <= $signed(pp12) + ($signed({{17{pp13[34]}}, pp13}) <<< 17);
  lo_all <= $signed(lo01) + ($signed({{33{lo23[58]}}, lo23}) <<< 34);
  hi_all <= $signed(hi01) + ($signed({{33{hi23[51]}}, hi23}) <<< 34);
  product <= $signed(lo_all) + ($signed({{23{hi_all[84]}}, hi_all}) <<< 23);
  p <= product;
  v <= {v[3:0], go};
  done <= v[4];
 end
 initial v = 0;
endmodule

// Unsigned restoring divider: 64-bit quotient, all ones when num >= den << 64.
module sm_div(
 input  wire clk, input wire rst, input wire go,
 input  wire [103:0] num, input wire [76:0] den,
 output reg  [63:0] q, output reg done
);
 reg [76:0] rem, d;
 reg [63:0] low;
 reg [6:0]  n;
 reg busy;
 wire [77:0] r = {rem, low[63]};
 wire [78:0] diff = {1'b0, r} - {2'b00, d};
 always @(posedge clk) begin
  done <= 1'b0;
  if (rst) begin
   busy <= 1'b0;
  end else if (go) begin
   if (~|den[76:40] && num[103:64] >= den[39:0]) begin
    q <= 64'hffffffffffffffff;
    done <= 1'b1;
   end else begin
    rem <= {37'd0, num[103:64]};
    low <= num[63:0];
    d <= den;
    n <= 7'd64;
    busy <= 1'b1;
   end
  end else if (busy) begin
   low <= low << 1;
   rem <= diff[78] ? r[76:0] : diff[76:0];
   q <= {q[62:0], ~diff[78]};
   n <= n - 1;
   if (n == 1) begin
    busy <= 1'b0;
    done <= 1'b1;
   end
  end
 end
endmodule

// Floor square root of a 64-bit value, two bits per cycle.
module sm_sqrt(
 input  wire clk, input wire rst, input wire go,
 input  wire [63:0] v,
 output reg  [31:0] r, output reg done
);
 reg [63:0] x;
 reg [35:0] rem;
 reg [5:0]  n;
 reg busy;
 always @(posedge clk) begin : body
  reg [35:0] rr, trial;
  done <= 1'b0;
  if (rst) begin
   busy <= 1'b0;
  end else if (go) begin
   x <= v; rem <= 0; r <= 0; n <= 6'd32; busy <= 1'b1;
  end else if (busy) begin
   rr = {rem[33:0], x[63:62]};
   trial = {2'b00, r, 2'b01};
   x <= x << 2;
   if (rr >= trial) begin
    rem <= rr - trial;
    r <= {r[30:0], 1'b1};
   end else begin
    rem <= rr;
    r <= {r[30:0], 1'b0};
   end
   n <= n - 1;
   if (n == 1) begin
    busy <= 1'b0;
    done <= 1'b1;
   end
  end
 end
endmodule
`default_nettype wire
