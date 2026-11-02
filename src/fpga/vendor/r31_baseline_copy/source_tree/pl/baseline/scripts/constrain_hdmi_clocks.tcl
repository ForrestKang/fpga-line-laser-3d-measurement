# The pinned rgb2dvi IP assumes an internally generated 5x clock even when
# kGenerateSerialClk=false. In this design BUFIO and BUFR are fed by the same
# axi_dynclk MMCM output. Bind SerialClk to its actual BUFIO source, not to a
# fictitious PixelClk -> SerialClk logic path. No timing path is disabled.
if {$::p2_mode in {camera_raw_hdmi structured_light_profile_hdmi}} {
    set hdmi_serial [get_pins bd_i/hdmi_out/rgb2dvi_0/U0/SerialClk]
    set hdmi_source [get_pins bd_i/hdmi_out/axi_dynclk/U0/BUFIO_inst/I]
    set hdmi_driver [get_pins -leaf -of_objects [get_nets -of_objects $hdmi_serial] -filter {DIRECTION == OUT}]
    if {[llength $hdmi_serial] != 1 || [llength $hdmi_source] != 1 ||
        $hdmi_driver ne "bd_i/hdmi_out/axi_dynclk/U0/BUFIO_inst/O"} {
        error "HDMI SerialClk physical clock source differs from reviewed topology"
    }
    create_generated_clock -name bd_i/hdmi_out/rgb2dvi_0/U0/SerialClk \
        -source $hdmi_source -divide_by 1 $hdmi_serial
}
