# R40 b09 copy of constrain_cdc_r37.tcl (only the configuration mailbox count differs).
# R37 iter_02 private CDC constraints. Do not edit shared R31/R36 or iter_01.
# This file is sourced after opening the frozen A16 synth checkpoint.
# All functional CDC exceptions below are scoped to explicit first-stage D pins.
if {![info exists p2_out] || ![file isdirectory $p2_out]} {
    error "p2_out must name an existing build report directory"
}
set cdc_inventory [file join $p2_out cdc_constraint_inventory.txt]
if {[file exists $cdc_inventory]} {error "CDC inventory already exists: $cdc_inventory"}
set cdc_fh [open $cdc_inventory {WRONLY CREAT EXCL}]

proc p2_cdc_dest {expression label expected} {
    global cdc_fh
    set cells [get_cells -quiet -hier -regexp $expression]
    set pins [get_pins -quiet -of_objects $cells -filter {REF_PIN_NAME == D}]
    puts $cdc_fh "$label expected_D=$expected actual_D=[llength $pins]"
    if {[llength $pins] != $expected} {
        error "$label expected $expected D pins, found [llength $pins]"
    }
    foreach p $pins {puts $cdc_fh "  DEST_D $p"}
    return $pins
}

proc r37_ff_clock_name {pin label} {
    set cell [get_cells -quiet -of_objects $pin]
    if {[llength $cell] != 1} {error "$label pin has no unique cell: $pin"}
    set cpin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]
    if {[llength $cpin] != 1} {error "$label FF has no unique C pin: $cell"}
    set clocks [get_clocks -quiet -of_objects $cpin]
    if {[llength $clocks] != 1} {error "$label FF C pin has no unique clock: $cpin"}
    return [get_property NAME [lindex $clocks 0]]
}

proc r37_direct_constant {d} {
    set nets [get_nets -quiet -of_objects $d]
    if {[llength $nets] != 1} {return {}}
    set drivers [get_pins -quiet -of_objects $nets -filter {DIRECTION == OUT}]
    if {[llength $drivers] != 1} {return {}}
    set cells [get_cells -quiet -of_objects $drivers]
    if {[llength $cells] != 1} {return {}}
    set ref [get_property REF_NAME [lindex $cells 0]]
    if {$ref eq "GND" || $ref eq "VCC"} {
        return [list $ref [lindex $drivers 0]]
    }
    return {}
}

# Verify every D's real launch Q and clock. Time each registered source from
# its legal source FF C pin to that exact D; record Q too for opt path auditing.
proc r37_registered_datapath {src_clock_name dst_clock_name destinations label use_skew} {
    global cdc_fh
    if {![llength $destinations]} {error "$label has no destination D pins"}
    set src_clock [get_clocks -quiet $src_clock_name]
    set dst_clock [get_clocks -quiet $dst_clock_name]
    if {[llength $src_clock] != 1 || [llength $dst_clock] != 1} {
        error "$label requires exact clocks $src_clock_name -> $dst_clock_name"
    }
    set all_q {}
    set all_c {}
    set active_d {}
    set constants 0
    set local_feedback 0
    foreach d $destinations {
        set d_name [get_property NAME $d]
        set d_cell [get_cells -quiet -of_objects $d]
        if {[llength $d_cell] != 1 || ![string match FD* [get_property REF_NAME $d_cell]]} {
            error "$label endpoint is not a single FF D pin: $d"
        }
        set cpin [get_pins -quiet -of_objects $d_cell -filter {REF_PIN_NAME == C}]
        if {[llength $cpin] != 1} {error "$label destination FF has no unique C pin: $d_cell"}
        set actual_dst [get_clocks -quiet -of_objects $cpin]
        if {[llength $actual_dst] != 1 ||
            [get_property NAME [lindex $actual_dst 0]] ne $dst_clock_name} {
            error "$label destination clock mismatch at $d_name: $actual_dst"
        }

        set direct_const [r37_direct_constant $d]
        if {[llength $direct_const]} {
            lassign $direct_const const_ref const_pin
            set starts [all_fanin -flat -startpoints_only -to $d]
            if {[llength $starts] > 1 ||
                ([llength $starts] == 1 &&
                [get_property NAME [lindex $starts 0]] ne [get_property NAME $const_pin])} {
                error "$label constant D is not a direct GND/VCC source: $d starts=$starts"
            }
            puts $cdc_fh "$label CONSTANT_D $d_name $const_ref [get_property NAME $const_pin]"
            incr constants
            continue
        }

        set launches [all_fanin -flat -startpoints_only -to $d]
        set d_q {}
        set d_c {}
        set found_cross 0
        foreach launch $launches {
            if {[get_property CLASS $launch] ne "pin" ||
                [get_property REF_PIN_NAME $launch] ni {C Q}} {
                error "$label unrecognized non-FF startpoint at $d_name: $launch"
            }
            set source_cell [get_cells -quiet -of_objects $launch]
            if {[llength $source_cell] != 1 ||
                ![string match FD* [get_property REF_NAME $source_cell]]} {
                error "$label launch is not a registered FF Q at $d_name: $launch"
            }
            set source_clock [r37_ff_clock_name $launch "$label launch"]
            set qpin [get_pins -quiet -of_objects $source_cell -filter {REF_PIN_NAME == Q}]
            set cone [all_fanin -flat -to $d]
            if {[llength $qpin] != 1 ||
                [lsearch -exact $cone [lindex $qpin 0]] < 0} {
                error "$label source Q does not resolve uniquely: $launch"
            }
            if {$source_clock eq $src_clock_name} {
                set source_c [get_pins -quiet -of_objects $source_cell -filter {REF_PIN_NAME == C}]
                if {[llength $source_c] != 1} {error "$label source C missing: $source_cell"}
                lappend d_q [lindex $qpin 0]
                lappend d_c [lindex $source_c 0]
                incr found_cross
            } elseif {$source_clock eq $dst_clock_name} {
                incr local_feedback
                puts $cdc_fh "$label LOCAL_FEEDBACK $d_name $launch clock=$source_clock"
            } else {
                error "$label third-clock launch at $d_name: $launch clock=$source_clock"
            }
        }
        if {!$found_cross} {
            error "$label D has no registered $src_clock_name launch: $d_name"
        }
        set d_q [lsort -unique $d_q]
        set d_c [lsort -unique $d_c]
        # Separate command per destination keeps the endpoint scope auditable.
        set_max_delay -datapath_only 10.0 -from $d_c -to $d
        lappend all_q {*}$d_q
        lappend all_c {*}$d_c
        lappend active_d $d
        puts $cdc_fh "$label TIMED_D $d_name Q=[join $d_q ,] C=[join $d_c ,]"
    }
    set all_q [lsort -unique $all_q]
    set all_c [lsort -unique $all_c]
    set active_d [lsort -unique $active_d]
    if {[llength $all_q] == 0 && $constants == 0} {error "$label has no verified sources"}
    if {$use_skew && [llength $active_d] > 1} {
        if {[llength $all_c] < 2} {error "$label bus skew lacks multiple registered source C pins"}
        set_bus_skew 10.0 -from $all_c -to $active_d
    }
    set record [dict create label $label clock $src_clock_name \
        capture_clock $dst_clock_name sources $all_q timing_starts $all_c \
        destinations $active_d]
    lappend ::r37_cdc_groups $record
    puts $cdc_fh "$label source_clock=$src_clock_name destination_clock=$dst_clock_name \
        D=[llength $destinations] timed_D=[llength $active_d] source_Q=[llength $all_q] \
        timing_start_C=[llength $all_c] constants=$constants local_feedback=$local_feedback \
        max_delay_ns=10 bus_skew=[expr {$use_skew && [llength $active_d] > 1 ? 10 : 0}]"
}

set ::r37_cdc_groups {}
set sys [get_clocks -quiet clk_fpga_0]
set cam [get_clocks -quiet cam_pclk]
if {[llength $sys] != 1 || [llength $cam] != 1} {
    error "Expected exactly one clk_fpga_0 and one cam_pclk clock"
}

# Camera-to-system core synchronizers and coherent geometry mailbox.
foreach {name count} {
    cam_fault_sync1 1
    cam_fault_flags_sync1 16
    cam_frame_count_sync1 16
    geometry_toggle_sync1 1
    geometry_line_sync1 32
    geometry_frame_sync1 11
    geometry_eof_sync1 5
    cam_global_reset_sync1 1
    cam_flush_reset_sync1 1
} {
    set pins [p2_cdc_dest [format {.*u_core/%s_reg(\[[0-9]+\])?} $name] $name $count]
    set skew [expr {$count > 1}]
    r37_registered_datapath cam_pclk clk_fpga_0 $pins $name $skew
}

# System request and held configuration mailbox into camera domain.
set req [p2_cdc_dest {.*u_core/req_sync1_reg} request_toggle_first_stage 1]
r37_registered_datapath clk_fpga_0 cam_pclk $req request_toggle 0
set config_expr [join [list \
    {.*u_core/cam_} \
    {(profile_id|epoch|threshold|peak|x0|x1|y0|y1|minw|maxw|sum|selected_mode)} \
    {_reg(\[[0-9]+\])?} \
] ""]
# R40 b09: PROFILE_ENABLE=0 trims the row-algorithm settings (threshold, peak, ROI,
# widths, sum, selected mode: 94 bits); cam_profile_id (32) and cam_epoch (16) remain.
set config [p2_cdc_dest $config_expr held_configuration_mailbox 48]
r37_registered_datapath clk_fpga_0 cam_pclk $config configuration_mailbox 1

# SOF metadata: toggle plus one 48-bit identity mailbox.
set meta_toggle [p2_cdc_dest {.*u_metadata/toggle_sync1_reg} metadata_sof_toggle_first_stage 1]
r37_registered_datapath cam_pclk clk_fpga_0 $meta_toggle metadata_sof_toggle 0
set meta_data [p2_cdc_dest {.*u_metadata/(id|epoch)_sync1_reg(\[[0-9]+\])?} metadata_sof_identity_mailbox 48]
r37_registered_datapath cam_pclk clk_fpga_0 $meta_data metadata_sof_identity 1

# PCLK Gray counter: first system stage only; gray_sync_2 is not a crossing.
set gray [p2_cdc_dest {.*u_pclk_meter/gray_sync_1_reg\[[0-9]+\]} pclk_gray_first_stage 32]
r37_registered_datapath cam_pclk clk_fpga_0 $gray pclk_gray 1

# Passive monitor command/ack/result mailboxes.
set mon_cmd_sync [p2_cdc_dest {.*u_signal_monitor/cmd_sync1_reg} monitor_command_toggle_first_stage 1]
r37_registered_datapath clk_fpga_0 cam_pclk $mon_cmd_sync monitor_command_toggle 0
set mon_ack_sync [p2_cdc_dest {.*u_signal_monitor/cmd_ack_sync1_reg} monitor_ack_toggle_first_stage 1]
r37_registered_datapath cam_pclk clk_fpga_0 $mon_ack_sync monitor_ack_toggle 0
set mon_done_sync [p2_cdc_dest {.*u_signal_monitor/done_sync1_reg} monitor_done_toggle_first_stage 1]
r37_registered_datapath cam_pclk clk_fpga_0 $mon_done_sync monitor_done_toggle 0
set mon_cmd_expr [join [list \
    {.*u_signal_monitor/cmd_(start|result_id)_stage_cam_reg} \
    {(\[[0-9]+\])?} \
] ""]
set mon_cmd_dst [p2_cdc_dest $mon_cmd_expr monitor_command_payload_capture 33]
r37_registered_datapath clk_fpga_0 cam_pclk $mon_cmd_dst monitor_command_payload 1
set mon_result_fields [join [list \
    result_id error_flags fv_rises fv_falls lv_rises lv_falls \
    fv_high_cycles fv_low_cycles lv_high_cycles lv_low_cycles \
    line_min line_max rows_min rows_max frames period_min period_max \
    pixel_min pixel_max \
] |]
set mon_result_expr [format {.*u_signal_monitor/done_(%s)_stage_sys_reg(\[[0-9]+\])?} $mon_result_fields]
set mon_result_dst [p2_cdc_dest $mon_result_expr monitor_result_payload_capture 501]
r37_registered_datapath cam_pclk clk_fpga_0 $mon_result_dst monitor_result_counters 1
set mon_variation_expr [format {.*u_signal_monitor/done_data_variation_stage_sys_reg\[[0-9]+\]}]
set mon_variation_dst [p2_cdc_dest $mon_variation_expr monitor_result_data_variation_capture 8]
r37_registered_datapath cam_pclk clk_fpga_0 $mon_variation_dst monitor_result_data_variation 1

# XPM async-reset cells own their PRE/CLR exception semantics. Inventory them
# exactly, but do not add a duplicate false path or broaden the reset scope.
foreach {label expression expected} {
    core_global_reset {.*u_core/.*u_global_reset/arststages_ff_reg\[([0-9]+)\]$} 2
    capture_global_reset {.*u_core/.*u_capture_global_reset/arststages_ff_reg\[([0-9]+)\]$} 2
    capture_flush_reset {.*u_core/.*u_capture_reset/arststages_ff_reg\[([0-9]+)\]$} 4
    meter_request_reset {.*u_pclk_meter/.*u_reset/arststages_ff_reg\[([0-9]+)\]$} 2
} {
    set cells [get_cells -quiet -hier -regexp $expression]
    set reset_pins [get_pins -quiet -of_objects $cells -filter {REF_PIN_NAME == PRE || REF_PIN_NAME == CLR}]
    if {[llength $cells] != $expected || [llength $reset_pins] != $expected} {
        error [format "%s XPM reset inventory mismatch: cells=%d pins=%d expected=%d" \
            $label [llength $cells] [llength $reset_pins] $expected]
    }
    set indexes {}
    foreach cell $cells {
        set name [get_property NAME $cell]
        if {![regexp {arststages_ff_reg\[([0-9]+)\]$} $name -> index]} {
            error "$label unexpected reset synchronizer cell: $name"
        }
        lappend indexes $index
        set pin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == PRE || REF_PIN_NAME == CLR}]
        if {[llength $pin] != 1} {error "$label cell must have exactly one PRE/CLR pin: $cell"}
        puts $cdc_fh "$label XPM_RESET_CELL $name [get_property REF_PIN_NAME $pin]"
    }
    if {[llength [lsort -unique $indexes]] != $expected} {error "$label duplicate XPM stage index: $indexes"}
    set sorted_indexes [join [lsort -integer $indexes] ,]
    puts $cdc_fh [format "%s XPM_RESET_COUNT cells=%d PRE_CLR=%d indices=%s no_extra_exception=1" \
        $label $expected $expected $sorted_indexes]
}

# SCCB inputs have no synchronous external clock. Only the first D is excepted.
foreach {port expression label} {
    cam_scl {.*(scl_sync1|scl_sync_1)_reg} sccb_scl_first_stage
    cam_sda {.*(sda_sync1|sda_sync_1)_reg} sccb_sda_first_stage
} {
    set port_obj [get_ports -quiet $port]
    if {[llength $port_obj] != 1} {error "$port external SCCB port missing or ambiguous"}
    set pins [p2_cdc_dest $expression $label 1]
    set_false_path -from $port_obj -to $pins
}

# R42 adds exactly four independent async inputs; only the first D is excepted.
set r42_first [p2_cdc_dest {.*buttons/sync1_reg\[[0-3]\]} r42_buttons_first_stage 4]
set r42_second [p2_cdc_dest {.*buttons/sync2_reg\[[0-3]\]} r42_buttons_second_stage 4]
foreach p [concat $r42_first $r42_second] {
 set c [get_cells -of_objects $p]
 set async_value [get_property ASYNC_REG $c]
 if {![string is boolean -strict $async_value]} {error "R42 ASYNC_REG missing/nonboolean: $c value=$async_value"}
 if {![expr {bool($async_value)}]} {error "R42 ASYNC_REG false: $c value=$async_value"}
 puts $cdc_fh "R42_BUTTON_SYNC cell=$c ASYNC_REG=$async_value"
}
for {set k 0} {$k < 4} {incr k} {
 set port [get_ports -quiet [format {btn[%d]} $k]]
 set d [get_pins -quiet -hier -regexp [format {.*buttons/sync1_reg\[%d\]/D} $k]]
 if {[llength $port]!=1 || [llength $d]!=1} {error "R42 button $k endpoint mismatch"}
 set_false_path -from $port -to $d
 puts $cdc_fh "R42_BUTTON_ASYNC port=$port first_D=$d"
}
close $cdc_fh
set endpoint_inventory [file join $p2_out cdc_endpoint_inventory.txt]
if {[file exists $endpoint_inventory]} {error "CDC endpoint inventory already exists"}
file copy $cdc_inventory $endpoint_inventory
report_exceptions -file [file join $p2_out cdc_exceptions.rpt]
