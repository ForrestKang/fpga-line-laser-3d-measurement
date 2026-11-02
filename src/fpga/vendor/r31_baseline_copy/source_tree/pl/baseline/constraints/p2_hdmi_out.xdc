# PYNQ-Z2 HDMI OUT pins, derived from the official PYNQ v3.1 Pynq-Z2 base XDC
# and cross-checked against D:\BoardDocumentation\PYNQ-Z2\Xilinx_PYNQ_base_reference.xdc.
# Source revision and individual file hashes are recorded under build/vendor/.
# The HPD net drives the board's HDMI detect buffer as in the official design.
set_property -dict {PACKAGE_PIN L17 IOSTANDARD TMDS_33} [get_ports hdmi_out_clk_n]
set_property -dict {PACKAGE_PIN L16 IOSTANDARD TMDS_33} [get_ports hdmi_out_clk_p]
set_property -dict {PACKAGE_PIN K18 IOSTANDARD TMDS_33} [get_ports {hdmi_out_data_n[0]}]
set_property -dict {PACKAGE_PIN K17 IOSTANDARD TMDS_33} [get_ports {hdmi_out_data_p[0]}]
set_property -dict {PACKAGE_PIN J19 IOSTANDARD TMDS_33} [get_ports {hdmi_out_data_n[1]}]
set_property -dict {PACKAGE_PIN K19 IOSTANDARD TMDS_33} [get_ports {hdmi_out_data_p[1]}]
set_property -dict {PACKAGE_PIN H18 IOSTANDARD TMDS_33} [get_ports {hdmi_out_data_n[2]}]
set_property -dict {PACKAGE_PIN J18 IOSTANDARD TMDS_33} [get_ports {hdmi_out_data_p[2]}]
set_property -dict {PACKAGE_PIN R19 IOSTANDARD LVCMOS33} [get_ports hdmi_out_hpd]
