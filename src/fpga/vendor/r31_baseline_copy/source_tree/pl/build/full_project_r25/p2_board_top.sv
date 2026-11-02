`timescale 1ns/1ps
module p2_board_top (
    inout wire [14:0] DDR_addr, inout wire [2:0] DDR_ba,
    inout wire DDR_cas_n, DDR_ck_n, DDR_ck_p, DDR_cke, DDR_cs_n,
    inout wire [3:0] DDR_dm, inout wire [31:0] DDR_dq,
    inout wire [3:0] DDR_dqs_n, DDR_dqs_p,
    inout wire DDR_odt, DDR_ras_n, DDR_reset_n, DDR_we_n,
    inout wire FIXED_IO_ddr_vrn, FIXED_IO_ddr_vrp,
    inout wire [53:0] FIXED_IO_mio,
    inout wire FIXED_IO_ps_clk, FIXED_IO_ps_porb, FIXED_IO_ps_srstb,
    input wire cam_pclk, cam_fv, cam_lv,
    input wire [7:0] cam_data,
    output wire cam_reset_n, cam_standby,
    inout wire cam_scl, cam_sda
    , output wire hdmi_out_clk_p, hdmi_out_clk_n,
    output wire [2:0] hdmi_out_data_p, hdmi_out_data_n,
    output wire hdmi_out_hpd
);
    wire cam_clk_ibuf, cam_clk;
    wire scl_i, scl_o, scl_t, sda_i, sda_o, sda_t;
    IBUF pclk_ibuf (.I(cam_pclk), .O(cam_clk_ibuf));
    BUFG pclk_bufg (.I(cam_clk_ibuf), .O(cam_clk));
    IOBUF scl_iobuf (.IO(cam_scl), .I(scl_o), .T(scl_t), .O(scl_i));
    IOBUF sda_iobuf (.IO(cam_sda), .I(sda_o), .T(sda_t), .O(sda_i));
    sl_profile_hdmi bd_i (
        .DDR_addr(DDR_addr), .DDR_ba(DDR_ba), .DDR_cas_n(DDR_cas_n),
        .DDR_ck_n(DDR_ck_n), .DDR_ck_p(DDR_ck_p), .DDR_cke(DDR_cke),
        .DDR_cs_n(DDR_cs_n), .DDR_dm(DDR_dm), .DDR_dq(DDR_dq),
        .DDR_dqs_n(DDR_dqs_n), .DDR_dqs_p(DDR_dqs_p),
        .DDR_odt(DDR_odt), .DDR_ras_n(DDR_ras_n), .DDR_reset_n(DDR_reset_n), .DDR_we_n(DDR_we_n),
        .FIXED_IO_ddr_vrn(FIXED_IO_ddr_vrn), .FIXED_IO_ddr_vrp(FIXED_IO_ddr_vrp),
        .FIXED_IO_mio(FIXED_IO_mio), .FIXED_IO_ps_clk(FIXED_IO_ps_clk),
        .FIXED_IO_ps_porb(FIXED_IO_ps_porb), .FIXED_IO_ps_srstb(FIXED_IO_ps_srstb),
        .cam_pclk(cam_clk), .cam_fv(cam_fv), .cam_lv(cam_lv), .cam_data(cam_data),
        .cam_reset_n(cam_reset_n), .cam_standby(cam_standby),
        .cam_scl_i(scl_i), .cam_scl_o(scl_o), .cam_scl_t(scl_t),
        .cam_sda_i(sda_i), .cam_sda_o(sda_o), .cam_sda_t(sda_t)
        , .hdmi_out_clk_p(hdmi_out_clk_p), .hdmi_out_clk_n(hdmi_out_clk_n),
        .hdmi_out_data_p(hdmi_out_data_p), .hdmi_out_data_n(hdmi_out_data_n),
        .hdmi_out_hpd(hdmi_out_hpd)

    );
endmodule
