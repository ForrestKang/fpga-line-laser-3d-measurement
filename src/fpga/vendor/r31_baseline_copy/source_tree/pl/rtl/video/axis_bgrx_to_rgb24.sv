`timescale 1ns/1ps
// PYNQ-Z2 HDMI OUT adapter. AXI VDMA's MM2S stream is configured as 32 bit;
// the PS stores each grayscale display pixel as B,G,R,0 in little-endian
// memory, so TDATA[23:0] is {R,G,B}. The source image uses equal RGB channels.
// This one-beat adapter removes the pad byte without changing framing or
// handshake semantics. TUSER[0] is start-of-frame; TLAST is end-of-line.
module axis_bgrx_to_rgb24 (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK",
       X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXIS:M_AXIS, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST",
       X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TDATA" *)
    input  wire [31:0] s_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TVALID" *)
    input  wire        s_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TREADY" *)
    output wire        s_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TLAST" *)
    input  wire        s_axis_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TUSER" *)
    input  wire        s_axis_tuser,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TDATA" *)
    output wire [23:0] m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TVALID" *)
    output wire        m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TREADY" *)
    input  wire        m_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TLAST" *)
    output wire        m_axis_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TUSER" *)
    output wire        m_axis_tuser
);
    // aclk/aresetn are interface metadata only: this pass-through transform
    // contains no state and therefore cannot retain or lose a beat on reset.
    wire unused_clk_reset = aclk ^ aresetn;
    assign m_axis_tdata   = s_axis_tdata[23:0];
    assign m_axis_tvalid  = s_axis_tvalid;
    assign s_axis_tready  = m_axis_tready;
    assign m_axis_tlast   = s_axis_tlast;
    assign m_axis_tuser   = s_axis_tuser;
endmodule
