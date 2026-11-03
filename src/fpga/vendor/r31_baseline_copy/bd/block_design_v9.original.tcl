
################################################################
# This is a generated script based on design: sl_profile_hdmi
#
# Though there are limitations about the generated script,
# the main purpose of this utility is to make learning
# IP Integrator Tcl commands easier.
################################################################

namespace eval _tcl {
proc get_script_folder {} {
   set script_path [file normalize [info script]]
   set script_folder [file dirname $script_path]
   return $script_folder
}
}
variable script_folder
set script_folder [_tcl::get_script_folder]

################################################################
# Check if script is running in correct Vivado version.
################################################################
set scripts_vivado_version 2025.2
set current_vivado_version [version -short]

if { [string first $scripts_vivado_version $current_vivado_version] == -1 } {
   puts ""
   if { [string compare $scripts_vivado_version $current_vivado_version] > 0 } {
      catch {common::send_gid_msg -ssname BD::TCL -id 2042 -severity "ERROR" " This script was generated using Vivado <$scripts_vivado_version> and is being run in <$current_vivado_version> of Vivado. Sourcing the script failed since it was created with a future version of Vivado."}

   } else {
     catch {common::send_gid_msg -ssname BD::TCL -id 2041 -severity "ERROR" "This script was generated using Vivado <$scripts_vivado_version> and is being run in <$current_vivado_version> of Vivado. Please run the script in Vivado <$scripts_vivado_version> then open the design in Vivado <$current_vivado_version>. Upgrade the design by running \"Tools => Report => Report IP Status...\", then run write_bd_tcl to create an updated script."}

   }

   return 1
}

################################################################
# START
################################################################

# To test this script, run the following commands from Vivado Tcl console:
# source sl_profile_hdmi_script.tcl


# The design that will be created by this Tcl script contains the following 
# module references:
# p2_processing_bd, motor_control_bd, b_motion_guard_bd, pfo3_axi_bd, axis_bgrx_to_rgb24_bd_wrapper

# Please add the sources of those modules before sourcing this Tcl script.

# If there is no project opened, this script will create a
# project, but make sure you do not have an existing project
# <./myproj/project_1.xpr> in the current working folder.

set list_projs [get_projects -quiet]
if { $list_projs eq "" } {
   create_project project_1 myproj -part xc7z020clg400-1
}


# CHANGE DESIGN NAME HERE
variable design_name
set design_name sl_profile_hdmi

# If you do not already have an existing IP Integrator design open,
# you can create a design using the following command:
#    create_bd_design $design_name

# Creating design if needed
set errMsg ""
set nRet 0

set cur_design [current_bd_design -quiet]
set list_cells [get_bd_cells -quiet]

if { ${design_name} eq "" } {
   # USE CASES:
   #    1) Design_name not set

   set errMsg "Please set the variable <design_name> to a non-empty value."
   set nRet 1

} elseif { ${cur_design} ne "" && ${list_cells} eq "" } {
   # USE CASES:
   #    2): Current design opened AND is empty AND names same.
   #    3): Current design opened AND is empty AND names diff; design_name NOT in project.
   #    4): Current design opened AND is empty AND names diff; design_name exists in project.

   if { $cur_design ne $design_name } {
      common::send_gid_msg -ssname BD::TCL -id 2001 -severity "INFO" "Changing value of <design_name> from <$design_name> to <$cur_design> since current design is empty."
      set design_name [get_property NAME $cur_design]
   }
   common::send_gid_msg -ssname BD::TCL -id 2002 -severity "INFO" "Constructing design in IPI design <$cur_design>..."

} elseif { ${cur_design} ne "" && $list_cells ne "" && $cur_design eq $design_name } {
   # USE CASES:
   #    5) Current design opened AND has components AND same names.

   set errMsg "Design <$design_name> already exists in your project, please set the variable <design_name> to another value."
   set nRet 1
} elseif { [get_files -quiet ${design_name}.bd] ne "" } {
   # USE CASES: 
   #    6) Current opened design, has components, but diff names, design_name exists in project.
   #    7) No opened design, design_name exists in project.

   set errMsg "Design <$design_name> already exists in your project, please set the variable <design_name> to another value."
   set nRet 2

} else {
   # USE CASES:
   #    8) No opened design, design_name not in project.
   #    9) Current opened design, has components, but diff names, design_name not in project.

   common::send_gid_msg -ssname BD::TCL -id 2003 -severity "INFO" "Currently there is no design <$design_name> in project, so creating one..."

   create_bd_design $design_name

   common::send_gid_msg -ssname BD::TCL -id 2004 -severity "INFO" "Making design <$design_name> as current_bd_design."
   current_bd_design $design_name

}

common::send_gid_msg -ssname BD::TCL -id 2005 -severity "INFO" "Currently the variable <design_name> is equal to \"$design_name\"."

if { $nRet != 0 } {
   catch {common::send_gid_msg -ssname BD::TCL -id 2006 -severity "ERROR" $errMsg}
   return $nRet
}

set bCheckIPsPassed 1
##################################################################
# CHECK IPs
##################################################################
set bCheckIPs 1
if { $bCheckIPs == 1 } {
   set list_check_ips "\ 
xilinx.com:ip:processing_system7:5.5\
xilinx.com:ip:proc_sys_reset:5.0\
xilinx.com:ip:xlconstant:1.1\
xilinx.com:ip:smartconnect:1.0\
xilinx.com:ip:xlconcat:2.1\
xilinx.com:ip:axi_dma:7.1\
xilinx.com:ip:axi_vdma:6.3\
xilinx.com:ip:axis_register_slice:1.1\
xilinx.com:ip:v_axi4s_vid_out:4.0\
xilinx.com:ip:v_tc:6.2\
digilentinc.com:ip:rgb2dvi:1.2\
digilentinc.com:ip:axi_dynclk:1.0\
"

   set list_ips_missing ""
   common::send_gid_msg -ssname BD::TCL -id 2011 -severity "INFO" "Checking if the following IPs exist in the project's IP catalog: $list_check_ips ."

   foreach ip_vlnv $list_check_ips {
      set ip_obj [get_ipdefs -all $ip_vlnv]
      if { $ip_obj eq "" } {
         lappend list_ips_missing $ip_vlnv
      }
   }

   if { $list_ips_missing ne "" } {
      catch {common::send_gid_msg -ssname BD::TCL -id 2012 -severity "ERROR" "The following IPs are not found in the IP Catalog:\n  $list_ips_missing\n\nResolution: Please add the repository containing the IP(s) to the project." }
      set bCheckIPsPassed 0
   }

}

##################################################################
# CHECK Modules
##################################################################
set bCheckModules 1
if { $bCheckModules == 1 } {
   set list_check_mods "\ 
p2_processing_bd\
motor_control_bd\
b_motion_guard_bd\
pfo3_axi_bd\
axis_bgrx_to_rgb24_bd_wrapper\
"

   set list_mods_missing ""
   common::send_gid_msg -ssname BD::TCL -id 2020 -severity "INFO" "Checking if the following modules exist in the project's sources: $list_check_mods ."

   foreach mod_vlnv $list_check_mods {
      if { [can_resolve_reference $mod_vlnv] == 0 } {
         lappend list_mods_missing $mod_vlnv
      }
   }

   if { $list_mods_missing ne "" } {
      catch {common::send_gid_msg -ssname BD::TCL -id 2021 -severity "ERROR" "The following module(s) are not found in the project: $list_mods_missing" }
      common::send_gid_msg -ssname BD::TCL -id 2022 -severity "INFO" "Please add source files for the missing module(s) above."
      set bCheckIPsPassed 0
   }
}

if { $bCheckIPsPassed != 1 } {
  common::send_gid_msg -ssname BD::TCL -id 2023 -severity "WARNING" "Will not continue with creation of design due to the error(s) above."
  return 3
}

##################################################################
# DESIGN PROCs
##################################################################


# Hierarchical cell: hdmi_out
proc create_hier_cell_hdmi_out { parentCell nameHier } {

  variable script_folder

  if { $parentCell eq "" || $nameHier eq "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2092 -severity "ERROR" "create_hier_cell_hdmi_out() - Empty argument(s)!"}
     return
  }

  # Get object for parentCell
  set parentObj [get_bd_cells $parentCell]
  if { $parentObj == "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2090 -severity "ERROR" "Unable to find parent cell <$parentCell>!"}
     return
  }

  # Make sure parentObj is hier blk
  set parentType [get_property TYPE $parentObj]
  if { $parentType ne "hier" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2091 -severity "ERROR" "Parent <$parentObj> has TYPE = <$parentType>. Expected to be <hier>."}
     return
  }

  # Save current instance; Restore later
  set oldCurInst [current_bd_instance .]

  # Set parent object as current
  current_bd_instance $parentObj

  # Create cell and set as current instance
  set hier_obj [create_bd_cell -type hier $nameHier]
  current_bd_instance $hier_obj

  # Create interface pins
  create_bd_intf_pin -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_DYNCLK

  create_bd_intf_pin -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_VTC

  create_bd_intf_pin -mode Slave -vlnv xilinx.com:interface:axis_rtl:1.0 in_stream

  create_bd_intf_pin -mode Master -vlnv digilentinc.com:interface:tmds_rtl:1.0 TMDS


  # Create pins
  create_bd_pin -dir I -type clk clk_100M
  create_bd_pin -dir I -type clk clk_142M
  create_bd_pin -dir I -type rst resetn_100M
  create_bd_pin -dir I -type rst resetn_142M
  create_bd_pin -dir O -from 0 -to 0 hdmi_out_hpd
  create_bd_pin -dir O LOCKED_O

  # Create instance: video_pixel_unpack, and set properties
  set block_name axis_bgrx_to_rgb24_bd_wrapper
  set block_cell_name video_pixel_unpack
  if { [catch {set video_pixel_unpack [create_bd_cell -type module -reference $block_name $block_cell_name] } errmsg] } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2095 -severity "ERROR" "Unable to add referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   } elseif { $video_pixel_unpack eq "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2096 -severity "ERROR" "Unable to referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   }
  
  # Create instance: axis_register_slice_0, and set properties
  set axis_register_slice_0 [ create_bd_cell -type ip -vlnv xilinx.com:ip:axis_register_slice:1.1 axis_register_slice_0 ]
  set_property CONFIG.TUSER_WIDTH {1} $axis_register_slice_0


  # Create instance: v_axi4s_vid_out_0, and set properties
  set v_axi4s_vid_out_0 [ create_bd_cell -type ip -vlnv xilinx.com:ip:v_axi4s_vid_out:4.0 v_axi4s_vid_out_0 ]
  set_property -dict [list \
    CONFIG.C_ADDR_WIDTH {11} \
    CONFIG.C_HAS_ASYNC_CLK {1} \
    CONFIG.C_HYSTERESIS_LEVEL {1024} \
    CONFIG.C_VTG_MASTER_SLAVE {1} \
  ] $v_axi4s_vid_out_0


  # Create instance: vtc_out, and set properties
  set vtc_out [ create_bd_cell -type ip -vlnv xilinx.com:ip:v_tc:6.2 vtc_out ]
  set_property CONFIG.enable_detection {false} $vtc_out


  # Create instance: rgb2dvi_0, and set properties
  set rgb2dvi_0 [ create_bd_cell -type ip -vlnv digilentinc.com:ip:rgb2dvi:1.2 rgb2dvi_0 ]
  set_property -dict [list \
    CONFIG.kClkRange {2} \
    CONFIG.kGenerateSerialClk {false} \
    CONFIG.kRstActiveHigh {false} \
  ] $rgb2dvi_0


  # Create instance: axi_dynclk, and set properties
  set axi_dynclk [ create_bd_cell -type ip -vlnv digilentinc.com:ip:axi_dynclk:1.0 axi_dynclk ]

  # Create instance: video_enable, and set properties
  set video_enable [ create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 video_enable ]
  set_property -dict [list \
    CONFIG.CONST_VAL {1} \
    CONFIG.CONST_WIDTH {1} \
  ] $video_enable


  # Create instance: video_reset_clear, and set properties
  set video_reset_clear [ create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 video_reset_clear ]
  set_property -dict [list \
    CONFIG.CONST_VAL {1} \
    CONFIG.CONST_WIDTH {1} \
  ] $video_reset_clear


  # Create instance: video_pixel_reset, and set properties
  set video_pixel_reset [ create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 video_pixel_reset ]

  # Create interface connections
  connect_bd_intf_net -intf_net S_AXI_DYNCLK_1 [get_bd_intf_pins S_AXI_DYNCLK] [get_bd_intf_pins axi_dynclk/s00_axi]
  connect_bd_intf_net -intf_net S_AXI_VTC_1 [get_bd_intf_pins S_AXI_VTC] [get_bd_intf_pins vtc_out/ctrl]
  connect_bd_intf_net -intf_net axis_register_slice_0_M_AXIS [get_bd_intf_pins axis_register_slice_0/M_AXIS] [get_bd_intf_pins v_axi4s_vid_out_0/video_in]
  connect_bd_intf_net -intf_net in_stream_1 [get_bd_intf_pins in_stream] [get_bd_intf_pins video_pixel_unpack/S_AXIS]
  connect_bd_intf_net -intf_net rgb2dvi_0_TMDS [get_bd_intf_pins rgb2dvi_0/TMDS] [get_bd_intf_pins TMDS]
  connect_bd_intf_net -intf_net v_axi4s_vid_out_0_vid_io_out [get_bd_intf_pins v_axi4s_vid_out_0/vid_io_out] [get_bd_intf_pins rgb2dvi_0/RGB]
  connect_bd_intf_net -intf_net video_pixel_unpack_M_AXIS [get_bd_intf_pins video_pixel_unpack/M_AXIS] [get_bd_intf_pins axis_register_slice_0/S_AXIS]
  connect_bd_intf_net -intf_net vtc_out_vtiming_out [get_bd_intf_pins v_axi4s_vid_out_0/vtiming_in] [get_bd_intf_pins vtc_out/vtiming_out]

  # Create port connections
  connect_bd_net -net axi_dynclk_LOCKED_O  [get_bd_pins axi_dynclk/LOCKED_O] \
  [get_bd_pins video_pixel_reset/dcm_locked] \
  [get_bd_pins rgb2dvi_0/aRst_n] \
  [get_bd_pins LOCKED_O]
  connect_bd_net -net axi_dynclk_PXL_CLK_5X_O  [get_bd_pins axi_dynclk/PXL_CLK_5X_O] \
  [get_bd_pins rgb2dvi_0/SerialClk]
  connect_bd_net -net axi_dynclk_PXL_CLK_O  [get_bd_pins axi_dynclk/PXL_CLK_O] \
  [get_bd_pins video_pixel_reset/slowest_sync_clk] \
  [get_bd_pins rgb2dvi_0/PixelClk] \
  [get_bd_pins v_axi4s_vid_out_0/vid_io_out_clk] \
  [get_bd_pins vtc_out/clk]
  connect_bd_net -net clk_100M_1  [get_bd_pins clk_100M] \
  [get_bd_pins axi_dynclk/s00_axi_aclk] \
  [get_bd_pins axi_dynclk/REF_CLK_I] \
  [get_bd_pins vtc_out/s_axi_aclk]
  connect_bd_net -net clk_142M_1  [get_bd_pins clk_142M] \
  [get_bd_pins video_pixel_unpack/aclk] \
  [get_bd_pins axis_register_slice_0/aclk] \
  [get_bd_pins v_axi4s_vid_out_0/aclk]
  connect_bd_net -net resetn_100M_1  [get_bd_pins resetn_100M] \
  [get_bd_pins axi_dynclk/s00_axi_aresetn] \
  [get_bd_pins vtc_out/s_axi_aresetn]
  connect_bd_net -net resetn_142M_1  [get_bd_pins resetn_142M] \
  [get_bd_pins video_pixel_unpack/aresetn] \
  [get_bd_pins axis_register_slice_0/aresetn] \
  [get_bd_pins v_axi4s_vid_out_0/aresetn]
  connect_bd_net -net video_enable_dout  [get_bd_pins video_enable/dout] \
  [get_bd_pins v_axi4s_vid_out_0/aclken] \
  [get_bd_pins v_axi4s_vid_out_0/vid_io_out_ce] \
  [get_bd_pins vtc_out/clken] \
  [get_bd_pins vtc_out/s_axi_aclken] \
  [get_bd_pins vtc_out/gen_clken]
  connect_bd_net -net video_pixel_reset_peripheral_aresetn  [get_bd_pins video_pixel_reset/peripheral_aresetn] \
  [get_bd_pins vtc_out/resetn]
  connect_bd_net -net video_pixel_reset_peripheral_reset  [get_bd_pins video_pixel_reset/peripheral_reset] \
  [get_bd_pins v_axi4s_vid_out_0/vid_io_out_reset]
  connect_bd_net -net video_reset_clear_dout  [get_bd_pins video_reset_clear/dout] \
  [get_bd_pins video_pixel_reset/ext_reset_in]

  # Restore current instance
  current_bd_instance $oldCurInst
}


# Procedure to create entire design; Provide argument to make
# procedure reusable. If parentCell is "", will use root.
proc create_root_design { parentCell } {

  variable script_folder
  variable design_name

  if { $parentCell eq "" } {
     set parentCell [get_bd_cells /]
  }

  # Get object for parentCell
  set parentObj [get_bd_cells $parentCell]
  if { $parentObj == "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2090 -severity "ERROR" "Unable to find parent cell <$parentCell>!"}
     return
  }

  # Make sure parentObj is hier blk
  set parentType [get_property TYPE $parentObj]
  if { $parentType ne "hier" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2091 -severity "ERROR" "Parent <$parentObj> has TYPE = <$parentType>. Expected to be <hier>."}
     return
  }

  # Save current instance; Restore later
  set oldCurInst [current_bd_instance .]

  # Set parent object as current
  current_bd_instance $parentObj


  # Create interface ports
  set hdmi_out [ create_bd_intf_port -mode Master -vlnv digilentinc.com:interface:tmds_rtl:1.0 hdmi_out ]

  set DDR [ create_bd_intf_port -mode Master -vlnv xilinx.com:interface:ddrx_rtl:1.0 DDR ]

  set FIXED_IO [ create_bd_intf_port -mode Master -vlnv xilinx.com:display_processing_system7:fixedio_rtl:1.0 FIXED_IO ]


  # Create ports
  set hdmi_out_hpd [ create_bd_port -dir O -from 0 -to 0 hdmi_out_hpd ]
  set cam_pclk [ create_bd_port -dir I -type clk -freq_hz 24000000 cam_pclk ]
  set cam_fv [ create_bd_port -dir I cam_fv ]
  set cam_lv [ create_bd_port -dir I cam_lv ]
  set cam_data [ create_bd_port -dir I -from 7 -to 0 cam_data ]
  set cam_reset_n [ create_bd_port -dir O cam_reset_n ]
  set cam_standby [ create_bd_port -dir O cam_standby ]
  set cam_scl_i [ create_bd_port -dir I cam_scl_i ]
  set cam_scl_o [ create_bd_port -dir O cam_scl_o ]
  set cam_scl_t [ create_bd_port -dir O cam_scl_t ]
  set cam_sda_i [ create_bd_port -dir I cam_sda_i ]
  set cam_sda_o [ create_bd_port -dir O cam_sda_o ]
  set cam_sda_t [ create_bd_port -dir O cam_sda_t ]
  set motor_step [ create_bd_port -dir O motor_step ]
  set motor_dir [ create_bd_port -dir O motor_dir ]
  set motor_enable [ create_bd_port -dir O motor_enable ]
  set motor_heartbeat [ create_bd_port -dir O motor_heartbeat ]

  # Create instance: ps7, and set properties
  set ps7 [ create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7 ]
  set_property -dict [list \
    CONFIG.PCW_ACT_APU_PERIPHERAL_FREQMHZ {650.000000} \
    CONFIG.PCW_ACT_CAN0_PERIPHERAL_FREQMHZ {23.8095} \
    CONFIG.PCW_ACT_CAN1_PERIPHERAL_FREQMHZ {23.8095} \
    CONFIG.PCW_ACT_CAN_PERIPHERAL_FREQMHZ {10.000000} \
    CONFIG.PCW_ACT_DCI_PERIPHERAL_FREQMHZ {10.096154} \
    CONFIG.PCW_ACT_ENET0_PERIPHERAL_FREQMHZ {125.000000} \
    CONFIG.PCW_ACT_ENET1_PERIPHERAL_FREQMHZ {10.000000} \
    CONFIG.PCW_ACT_FPGA0_PERIPHERAL_FREQMHZ {100.000000} \
    CONFIG.PCW_ACT_FPGA1_PERIPHERAL_FREQMHZ {142.857132} \
    CONFIG.PCW_ACT_FPGA2_PERIPHERAL_FREQMHZ {10.000000} \
    CONFIG.PCW_ACT_FPGA3_PERIPHERAL_FREQMHZ {10.000000} \
    CONFIG.PCW_ACT_I2C_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_ACT_PCAP_PERIPHERAL_FREQMHZ {200.000000} \
    CONFIG.PCW_ACT_QSPI_PERIPHERAL_FREQMHZ {200.000000} \
    CONFIG.PCW_ACT_SDIO_PERIPHERAL_FREQMHZ {50.000000} \
    CONFIG.PCW_ACT_SMC_PERIPHERAL_FREQMHZ {10.000000} \
    CONFIG.PCW_ACT_SPI_PERIPHERAL_FREQMHZ {10.000000} \
    CONFIG.PCW_ACT_TPIU_PERIPHERAL_FREQMHZ {200.000000} \
    CONFIG.PCW_ACT_TTC0_CLK0_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_ACT_TTC0_CLK1_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_ACT_TTC0_CLK2_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_ACT_TTC1_CLK0_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_ACT_TTC1_CLK1_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_ACT_TTC1_CLK2_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_ACT_TTC_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_ACT_UART_PERIPHERAL_FREQMHZ {100.000000} \
    CONFIG.PCW_ACT_USB0_PERIPHERAL_FREQMHZ {60} \
    CONFIG.PCW_ACT_USB1_PERIPHERAL_FREQMHZ {60} \
    CONFIG.PCW_ACT_WDT_PERIPHERAL_FREQMHZ {108.333336} \
    CONFIG.PCW_APU_CLK_RATIO_ENABLE {6:2:1} \
    CONFIG.PCW_APU_PERIPHERAL_FREQMHZ {650} \
    CONFIG.PCW_CAN0_PERIPHERAL_CLKSRC {External} \
    CONFIG.PCW_CAN0_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_CAN1_PERIPHERAL_CLKSRC {External} \
    CONFIG.PCW_CAN1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_CAN_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_CAN_PERIPHERAL_VALID {0} \
    CONFIG.PCW_CLK0_FREQ {100000000} \
    CONFIG.PCW_CLK1_FREQ {142857132} \
    CONFIG.PCW_CLK2_FREQ {10000000} \
    CONFIG.PCW_CLK3_FREQ {10000000} \
    CONFIG.PCW_CORE0_FIQ_INTR {0} \
    CONFIG.PCW_CORE0_IRQ_INTR {0} \
    CONFIG.PCW_CORE1_FIQ_INTR {0} \
    CONFIG.PCW_CORE1_IRQ_INTR {0} \
    CONFIG.PCW_CPU_CPU_6X4X_MAX_RANGE {667} \
    CONFIG.PCW_CPU_PERIPHERAL_CLKSRC {ARM PLL} \
    CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_DCI_PERIPHERAL_CLKSRC {DDR PLL} \
    CONFIG.PCW_DCI_PERIPHERAL_FREQMHZ {10.159} \
    CONFIG.PCW_DDR_PERIPHERAL_CLKSRC {DDR PLL} \
    CONFIG.PCW_DDR_RAM_BASEADDR {0x00100000} \
    CONFIG.PCW_DDR_RAM_HIGHADDR {0x1FFFFFFF} \
    CONFIG.PCW_DM_WIDTH {4} \
    CONFIG.PCW_DQS_WIDTH {4} \
    CONFIG.PCW_DQ_WIDTH {32} \
    CONFIG.PCW_ENET0_BASEADDR {0xE000B000} \
    CONFIG.PCW_ENET0_ENET0_IO {MIO 16 .. 27} \
    CONFIG.PCW_ENET0_GRP_MDIO_ENABLE {1} \
    CONFIG.PCW_ENET0_GRP_MDIO_IO {MIO 52 .. 53} \
    CONFIG.PCW_ENET0_HIGHADDR {0xE000BFFF} \
    CONFIG.PCW_ENET0_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_ENET0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_ENET0_PERIPHERAL_FREQMHZ {1000 Mbps} \
    CONFIG.PCW_ENET1_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_ENET1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_ENET_RESET_POLARITY {Active Low} \
    CONFIG.PCW_EN_4K_TIMER {0} \
    CONFIG.PCW_EN_CAN0 {0} \
    CONFIG.PCW_EN_CAN1 {0} \
    CONFIG.PCW_EN_CLK0_PORT {1} \
    CONFIG.PCW_EN_CLK1_PORT {1} \
    CONFIG.PCW_EN_CLK2_PORT {0} \
    CONFIG.PCW_EN_CLK3_PORT {0} \
    CONFIG.PCW_EN_CLKTRIG0_PORT {0} \
    CONFIG.PCW_EN_CLKTRIG1_PORT {0} \
    CONFIG.PCW_EN_CLKTRIG2_PORT {0} \
    CONFIG.PCW_EN_CLKTRIG3_PORT {0} \
    CONFIG.PCW_EN_DDR {1} \
    CONFIG.PCW_EN_EMIO_CAN0 {0} \
    CONFIG.PCW_EN_EMIO_CAN1 {0} \
    CONFIG.PCW_EN_EMIO_CD_SDIO0 {0} \
    CONFIG.PCW_EN_EMIO_CD_SDIO1 {0} \
    CONFIG.PCW_EN_EMIO_ENET0 {0} \
    CONFIG.PCW_EN_EMIO_ENET1 {0} \
    CONFIG.PCW_EN_EMIO_GPIO {0} \
    CONFIG.PCW_EN_EMIO_I2C0 {0} \
    CONFIG.PCW_EN_EMIO_I2C1 {0} \
    CONFIG.PCW_EN_EMIO_MODEM_UART0 {0} \
    CONFIG.PCW_EN_EMIO_MODEM_UART1 {0} \
    CONFIG.PCW_EN_EMIO_PJTAG {0} \
    CONFIG.PCW_EN_EMIO_SDIO0 {0} \
    CONFIG.PCW_EN_EMIO_SDIO1 {0} \
    CONFIG.PCW_EN_EMIO_SPI0 {0} \
    CONFIG.PCW_EN_EMIO_SPI1 {0} \
    CONFIG.PCW_EN_EMIO_SRAM_INT {0} \
    CONFIG.PCW_EN_EMIO_TRACE {0} \
    CONFIG.PCW_EN_EMIO_TTC0 {0} \
    CONFIG.PCW_EN_EMIO_TTC1 {0} \
    CONFIG.PCW_EN_EMIO_UART0 {0} \
    CONFIG.PCW_EN_EMIO_UART1 {0} \
    CONFIG.PCW_EN_EMIO_WDT {0} \
    CONFIG.PCW_EN_EMIO_WP_SDIO0 {0} \
    CONFIG.PCW_EN_EMIO_WP_SDIO1 {0} \
    CONFIG.PCW_EN_ENET0 {1} \
    CONFIG.PCW_EN_ENET1 {0} \
    CONFIG.PCW_EN_GPIO {0} \
    CONFIG.PCW_EN_I2C0 {0} \
    CONFIG.PCW_EN_I2C1 {0} \
    CONFIG.PCW_EN_MODEM_UART0 {0} \
    CONFIG.PCW_EN_MODEM_UART1 {0} \
    CONFIG.PCW_EN_PJTAG {0} \
    CONFIG.PCW_EN_PTP_ENET0 {0} \
    CONFIG.PCW_EN_PTP_ENET1 {0} \
    CONFIG.PCW_EN_QSPI {1} \
    CONFIG.PCW_EN_RST0_PORT {1} \
    CONFIG.PCW_EN_RST1_PORT {0} \
    CONFIG.PCW_EN_RST2_PORT {0} \
    CONFIG.PCW_EN_RST3_PORT {0} \
    CONFIG.PCW_EN_SDIO0 {1} \
    CONFIG.PCW_EN_SDIO1 {0} \
    CONFIG.PCW_EN_SMC {0} \
    CONFIG.PCW_EN_SPI0 {0} \
    CONFIG.PCW_EN_SPI1 {0} \
    CONFIG.PCW_EN_TRACE {0} \
    CONFIG.PCW_EN_TTC0 {0} \
    CONFIG.PCW_EN_TTC1 {0} \
    CONFIG.PCW_EN_UART0 {1} \
    CONFIG.PCW_EN_UART1 {0} \
    CONFIG.PCW_EN_USB0 {1} \
    CONFIG.PCW_EN_USB1 {0} \
    CONFIG.PCW_EN_WDT {0} \
    CONFIG.PCW_FCLK0_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_FCLK1_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_FCLK2_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_FCLK3_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_FCLK_CLK0_BUF {TRUE} \
    CONFIG.PCW_FCLK_CLK1_BUF {TRUE} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ {142.857132} \
    CONFIG.PCW_FPGA2_PERIPHERAL_FREQMHZ {200} \
    CONFIG.PCW_FPGA3_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_FPGA_FCLK0_ENABLE {1} \
    CONFIG.PCW_FPGA_FCLK1_ENABLE {1} \
    CONFIG.PCW_GPIO_EMIO_GPIO_ENABLE {0} \
    CONFIG.PCW_GPIO_MIO_GPIO_ENABLE {0} \
    CONFIG.PCW_GPIO_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_I2C0_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_I2C1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_I2C_RESET_POLARITY {Active Low} \
    CONFIG.PCW_IMPORT_BOARD_PRESET {None} \
    CONFIG.PCW_INCLUDE_ACP_TRANS_CHECK {0} \
    CONFIG.PCW_IRQ_F2P_INTR {1} \
    CONFIG.PCW_IRQ_F2P_MODE {DIRECT} \
    CONFIG.PCW_MIO_14_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_14_PULLUP {enabled} \
    CONFIG.PCW_MIO_14_SLEW {slow} \
    CONFIG.PCW_MIO_15_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_15_PULLUP {enabled} \
    CONFIG.PCW_MIO_15_SLEW {slow} \
    CONFIG.PCW_MIO_16_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_16_PULLUP {enabled} \
    CONFIG.PCW_MIO_16_SLEW {slow} \
    CONFIG.PCW_MIO_17_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_17_PULLUP {enabled} \
    CONFIG.PCW_MIO_17_SLEW {slow} \
    CONFIG.PCW_MIO_18_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_18_PULLUP {enabled} \
    CONFIG.PCW_MIO_18_SLEW {slow} \
    CONFIG.PCW_MIO_19_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_19_PULLUP {enabled} \
    CONFIG.PCW_MIO_19_SLEW {slow} \
    CONFIG.PCW_MIO_1_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_1_PULLUP {enabled} \
    CONFIG.PCW_MIO_1_SLEW {slow} \
    CONFIG.PCW_MIO_20_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_20_PULLUP {enabled} \
    CONFIG.PCW_MIO_20_SLEW {slow} \
    CONFIG.PCW_MIO_21_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_21_PULLUP {enabled} \
    CONFIG.PCW_MIO_21_SLEW {slow} \
    CONFIG.PCW_MIO_22_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_22_PULLUP {enabled} \
    CONFIG.PCW_MIO_22_SLEW {slow} \
    CONFIG.PCW_MIO_23_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_23_PULLUP {enabled} \
    CONFIG.PCW_MIO_23_SLEW {slow} \
    CONFIG.PCW_MIO_24_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_24_PULLUP {enabled} \
    CONFIG.PCW_MIO_24_SLEW {slow} \
    CONFIG.PCW_MIO_25_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_25_PULLUP {enabled} \
    CONFIG.PCW_MIO_25_SLEW {slow} \
    CONFIG.PCW_MIO_26_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_26_PULLUP {enabled} \
    CONFIG.PCW_MIO_26_SLEW {slow} \
    CONFIG.PCW_MIO_27_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_27_PULLUP {enabled} \
    CONFIG.PCW_MIO_27_SLEW {slow} \
    CONFIG.PCW_MIO_28_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_28_PULLUP {enabled} \
    CONFIG.PCW_MIO_28_SLEW {slow} \
    CONFIG.PCW_MIO_29_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_29_PULLUP {enabled} \
    CONFIG.PCW_MIO_29_SLEW {slow} \
    CONFIG.PCW_MIO_2_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_2_SLEW {slow} \
    CONFIG.PCW_MIO_30_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_30_PULLUP {enabled} \
    CONFIG.PCW_MIO_30_SLEW {slow} \
    CONFIG.PCW_MIO_31_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_31_PULLUP {enabled} \
    CONFIG.PCW_MIO_31_SLEW {slow} \
    CONFIG.PCW_MIO_32_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_32_PULLUP {enabled} \
    CONFIG.PCW_MIO_32_SLEW {slow} \
    CONFIG.PCW_MIO_33_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_33_PULLUP {enabled} \
    CONFIG.PCW_MIO_33_SLEW {slow} \
    CONFIG.PCW_MIO_34_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_34_PULLUP {enabled} \
    CONFIG.PCW_MIO_34_SLEW {slow} \
    CONFIG.PCW_MIO_35_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_35_PULLUP {enabled} \
    CONFIG.PCW_MIO_35_SLEW {slow} \
    CONFIG.PCW_MIO_36_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_36_PULLUP {enabled} \
    CONFIG.PCW_MIO_36_SLEW {slow} \
    CONFIG.PCW_MIO_37_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_37_PULLUP {enabled} \
    CONFIG.PCW_MIO_37_SLEW {slow} \
    CONFIG.PCW_MIO_38_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_38_PULLUP {enabled} \
    CONFIG.PCW_MIO_38_SLEW {slow} \
    CONFIG.PCW_MIO_39_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_39_PULLUP {enabled} \
    CONFIG.PCW_MIO_39_SLEW {slow} \
    CONFIG.PCW_MIO_3_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_3_SLEW {slow} \
    CONFIG.PCW_MIO_40_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_40_PULLUP {enabled} \
    CONFIG.PCW_MIO_40_SLEW {slow} \
    CONFIG.PCW_MIO_41_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_41_PULLUP {enabled} \
    CONFIG.PCW_MIO_41_SLEW {slow} \
    CONFIG.PCW_MIO_42_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_42_PULLUP {enabled} \
    CONFIG.PCW_MIO_42_SLEW {slow} \
    CONFIG.PCW_MIO_43_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_43_PULLUP {enabled} \
    CONFIG.PCW_MIO_43_SLEW {slow} \
    CONFIG.PCW_MIO_44_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_44_PULLUP {enabled} \
    CONFIG.PCW_MIO_44_SLEW {slow} \
    CONFIG.PCW_MIO_45_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_45_PULLUP {enabled} \
    CONFIG.PCW_MIO_45_SLEW {slow} \
    CONFIG.PCW_MIO_47_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_47_PULLUP {enabled} \
    CONFIG.PCW_MIO_47_SLEW {slow} \
    CONFIG.PCW_MIO_4_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_4_SLEW {slow} \
    CONFIG.PCW_MIO_52_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_52_PULLUP {enabled} \
    CONFIG.PCW_MIO_52_SLEW {slow} \
    CONFIG.PCW_MIO_53_IOTYPE {LVCMOS 1.8V} \
    CONFIG.PCW_MIO_53_PULLUP {enabled} \
    CONFIG.PCW_MIO_53_SLEW {slow} \
    CONFIG.PCW_MIO_5_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_5_SLEW {slow} \
    CONFIG.PCW_MIO_6_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_6_SLEW {slow} \
    CONFIG.PCW_MIO_8_IOTYPE {LVCMOS 3.3V} \
    CONFIG.PCW_MIO_8_SLEW {slow} \
    CONFIG.PCW_MIO_PRIMITIVE {54} \
    CONFIG.PCW_MIO_TREE_PERIPHERALS {unassigned#Quad SPI Flash#Quad SPI Flash#Quad SPI Flash#Quad SPI Flash#Quad SPI Flash#Quad SPI Flash#unassigned#Quad SPI Flash#unassigned#unassigned#unassigned#unassigned#unassigned#UART\
0#UART 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#Enet 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#USB 0#SD 0#SD 0#SD 0#SD 0#SD 0#SD 0#unassigned#SD\
0#unassigned#unassigned#unassigned#unassigned#Enet 0#Enet 0} \
    CONFIG.PCW_MIO_TREE_SIGNALS {unassigned#qspi0_ss_b#qspi0_io[0]#qspi0_io[1]#qspi0_io[2]#qspi0_io[3]/HOLD_B#qspi0_sclk#unassigned#qspi_fbclk#unassigned#unassigned#unassigned#unassigned#unassigned#rx#tx#tx_clk#txd[0]#txd[1]#txd[2]#txd[3]#tx_ctl#rx_clk#rxd[0]#rxd[1]#rxd[2]#rxd[3]#rx_ctl#data[4]#dir#stp#nxt#data[0]#data[1]#data[2]#data[3]#clk#data[5]#data[6]#data[7]#clk#cmd#data[0]#data[1]#data[2]#data[3]#unassigned#cd#unassigned#unassigned#unassigned#unassigned#mdc#mdio}\
\
    CONFIG.PCW_M_AXI_GP0_ENABLE_STATIC_REMAP {0} \
    CONFIG.PCW_M_AXI_GP0_ID_WIDTH {12} \
    CONFIG.PCW_M_AXI_GP0_SUPPORT_NARROW_BURST {0} \
    CONFIG.PCW_M_AXI_GP0_THREAD_ID_WIDTH {12} \
    CONFIG.PCW_NAND_CYCLES_T_AR {1} \
    CONFIG.PCW_NAND_CYCLES_T_CLR {1} \
    CONFIG.PCW_NAND_CYCLES_T_RC {11} \
    CONFIG.PCW_NAND_CYCLES_T_REA {1} \
    CONFIG.PCW_NAND_CYCLES_T_RR {1} \
    CONFIG.PCW_NAND_CYCLES_T_WC {11} \
    CONFIG.PCW_NAND_CYCLES_T_WP {1} \
    CONFIG.PCW_NOR_CS0_T_CEOE {1} \
    CONFIG.PCW_NOR_CS0_T_PC {1} \
    CONFIG.PCW_NOR_CS0_T_RC {11} \
    CONFIG.PCW_NOR_CS0_T_TR {1} \
    CONFIG.PCW_NOR_CS0_T_WC {11} \
    CONFIG.PCW_NOR_CS0_T_WP {1} \
    CONFIG.PCW_NOR_CS0_WE_TIME {0} \
    CONFIG.PCW_NOR_CS1_T_CEOE {1} \
    CONFIG.PCW_NOR_CS1_T_PC {1} \
    CONFIG.PCW_NOR_CS1_T_RC {11} \
    CONFIG.PCW_NOR_CS1_T_TR {1} \
    CONFIG.PCW_NOR_CS1_T_WC {11} \
    CONFIG.PCW_NOR_CS1_T_WP {1} \
    CONFIG.PCW_NOR_CS1_WE_TIME {0} \
    CONFIG.PCW_NOR_SRAM_CS0_T_CEOE {1} \
    CONFIG.PCW_NOR_SRAM_CS0_T_PC {1} \
    CONFIG.PCW_NOR_SRAM_CS0_T_RC {11} \
    CONFIG.PCW_NOR_SRAM_CS0_T_TR {1} \
    CONFIG.PCW_NOR_SRAM_CS0_T_WC {11} \
    CONFIG.PCW_NOR_SRAM_CS0_T_WP {1} \
    CONFIG.PCW_NOR_SRAM_CS0_WE_TIME {0} \
    CONFIG.PCW_NOR_SRAM_CS1_T_CEOE {1} \
    CONFIG.PCW_NOR_SRAM_CS1_T_PC {1} \
    CONFIG.PCW_NOR_SRAM_CS1_T_RC {11} \
    CONFIG.PCW_NOR_SRAM_CS1_T_TR {1} \
    CONFIG.PCW_NOR_SRAM_CS1_T_WC {11} \
    CONFIG.PCW_NOR_SRAM_CS1_T_WP {1} \
    CONFIG.PCW_NOR_SRAM_CS1_WE_TIME {0} \
    CONFIG.PCW_OVERRIDE_BASIC_CLOCK {0} \
    CONFIG.PCW_P2F_ENET0_INTR {0} \
    CONFIG.PCW_P2F_QSPI_INTR {0} \
    CONFIG.PCW_P2F_SDIO0_INTR {0} \
    CONFIG.PCW_P2F_UART0_INTR {0} \
    CONFIG.PCW_P2F_USB0_INTR {0} \
    CONFIG.PCW_PACKAGE_DDR_BOARD_DELAY0 {0.223} \
    CONFIG.PCW_PACKAGE_DDR_BOARD_DELAY1 {0.212} \
    CONFIG.PCW_PACKAGE_DDR_BOARD_DELAY2 {0.085} \
    CONFIG.PCW_PACKAGE_DDR_BOARD_DELAY3 {0.092} \
    CONFIG.PCW_PACKAGE_DDR_DQS_TO_CLK_DELAY_0 {0.040} \
    CONFIG.PCW_PACKAGE_DDR_DQS_TO_CLK_DELAY_1 {0.058} \
    CONFIG.PCW_PACKAGE_DDR_DQS_TO_CLK_DELAY_2 {-0.009} \
    CONFIG.PCW_PACKAGE_DDR_DQS_TO_CLK_DELAY_3 {-0.033} \
    CONFIG.PCW_PACKAGE_NAME {clg400} \
    CONFIG.PCW_PCAP_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_PCAP_PERIPHERAL_FREQMHZ {200} \
    CONFIG.PCW_PERIPHERAL_BOARD_PRESET {None} \
    CONFIG.PCW_PJTAG_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_PLL_BYPASSMODE_ENABLE {0} \
    CONFIG.PCW_PRESET_BANK0_VOLTAGE {LVCMOS 3.3V} \
    CONFIG.PCW_PRESET_BANK1_VOLTAGE {LVCMOS 1.8V} \
    CONFIG.PCW_PS7_SI_REV {PRODUCTION} \
    CONFIG.PCW_QSPI_GRP_FBCLK_ENABLE {1} \
    CONFIG.PCW_QSPI_GRP_FBCLK_IO {MIO 8} \
    CONFIG.PCW_QSPI_GRP_IO1_ENABLE {0} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_ENABLE {1} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_IO {MIO 1 .. 6} \
    CONFIG.PCW_QSPI_GRP_SS1_ENABLE {0} \
    CONFIG.PCW_QSPI_INTERNAL_HIGHADDRESS {0xFCFFFFFF} \
    CONFIG.PCW_QSPI_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_QSPI_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_QSPI_PERIPHERAL_FREQMHZ {200} \
    CONFIG.PCW_QSPI_QSPI_IO {MIO 1 .. 6} \
    CONFIG.PCW_SD0_GRP_CD_ENABLE {1} \
    CONFIG.PCW_SD0_GRP_CD_IO {MIO 47} \
    CONFIG.PCW_SD0_GRP_POW_ENABLE {0} \
    CONFIG.PCW_SD0_GRP_WP_ENABLE {0} \
    CONFIG.PCW_SD0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_SD0_SD0_IO {MIO 40 .. 45} \
    CONFIG.PCW_SD1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_SDIO0_BASEADDR {0xE0100000} \
    CONFIG.PCW_SDIO0_HIGHADDR {0xE0100FFF} \
    CONFIG.PCW_SDIO_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_SDIO_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_SDIO_PERIPHERAL_VALID {1} \
    CONFIG.PCW_SINGLE_QSPI_DATA_MODE {x4} \
    CONFIG.PCW_SMC_CYCLE_T0 {NA} \
    CONFIG.PCW_SMC_CYCLE_T1 {NA} \
    CONFIG.PCW_SMC_CYCLE_T2 {NA} \
    CONFIG.PCW_SMC_CYCLE_T3 {NA} \
    CONFIG.PCW_SMC_CYCLE_T4 {NA} \
    CONFIG.PCW_SMC_CYCLE_T5 {NA} \
    CONFIG.PCW_SMC_CYCLE_T6 {NA} \
    CONFIG.PCW_SMC_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_SMC_PERIPHERAL_VALID {0} \
    CONFIG.PCW_SPI0_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_SPI1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_SPI_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_SPI_PERIPHERAL_VALID {0} \
    CONFIG.PCW_S_AXI_HP0_DATA_WIDTH {64} \
    CONFIG.PCW_S_AXI_HP0_ID_WIDTH {6} \
    CONFIG.PCW_S_AXI_HP1_DATA_WIDTH {64} \
    CONFIG.PCW_TPIU_PERIPHERAL_CLKSRC {External} \
    CONFIG.PCW_TRACE_INTERNAL_WIDTH {2} \
    CONFIG.PCW_TRACE_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_TTC0_CLK0_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_TTC0_CLK0_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_TTC0_CLK1_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_TTC0_CLK1_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_TTC0_CLK2_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_TTC0_CLK2_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_TTC0_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_TTC1_CLK0_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_TTC1_CLK0_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_TTC1_CLK1_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_TTC1_CLK1_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_TTC1_CLK2_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_TTC1_CLK2_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_TTC1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_UART0_BASEADDR {0xE0000000} \
    CONFIG.PCW_UART0_BAUD_RATE {115200} \
    CONFIG.PCW_UART0_GRP_FULL_ENABLE {0} \
    CONFIG.PCW_UART0_HIGHADDR {0xE0000FFF} \
    CONFIG.PCW_UART0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_UART0_UART0_IO {MIO 14 .. 15} \
    CONFIG.PCW_UART1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_UART_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_UART_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_UART_PERIPHERAL_VALID {1} \
    CONFIG.PCW_UIPARAM_ACT_DDR_FREQ_MHZ {525.000000} \
    CONFIG.PCW_UIPARAM_DDR_ADV_ENABLE {0} \
    CONFIG.PCW_UIPARAM_DDR_AL {0} \
    CONFIG.PCW_UIPARAM_DDR_BANK_ADDR_COUNT {3} \
    CONFIG.PCW_UIPARAM_DDR_BL {8} \
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY0 {0.223} \
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY1 {0.212} \
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY2 {0.085} \
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY3 {0.092} \
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH {16 Bit} \
    CONFIG.PCW_UIPARAM_DDR_CL {7} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_0_LENGTH_MM {25.8} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_0_PACKAGE_LENGTH {80.4535} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_0_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_1_LENGTH_MM {25.8} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_1_PACKAGE_LENGTH {80.4535} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_1_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_2_LENGTH_MM {0} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_2_PACKAGE_LENGTH {80.4535} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_2_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_3_LENGTH_MM {0} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_3_PACKAGE_LENGTH {80.4535} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_3_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_CLOCK_STOP_EN {0} \
    CONFIG.PCW_UIPARAM_DDR_COL_ADDR_COUNT {10} \
    CONFIG.PCW_UIPARAM_DDR_CWL {6} \
    CONFIG.PCW_UIPARAM_DDR_DEVICE_CAPACITY {4096 MBits} \
    CONFIG.PCW_UIPARAM_DDR_DQS_0_LENGTH_MM {15.6} \
    CONFIG.PCW_UIPARAM_DDR_DQS_0_PACKAGE_LENGTH {105.056} \
    CONFIG.PCW_UIPARAM_DDR_DQS_0_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQS_1_LENGTH_MM {18.8} \
    CONFIG.PCW_UIPARAM_DDR_DQS_1_PACKAGE_LENGTH {66.904} \
    CONFIG.PCW_UIPARAM_DDR_DQS_1_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQS_2_LENGTH_MM {0} \
    CONFIG.PCW_UIPARAM_DDR_DQS_2_PACKAGE_LENGTH {89.1715} \
    CONFIG.PCW_UIPARAM_DDR_DQS_2_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQS_3_LENGTH_MM {0} \
    CONFIG.PCW_UIPARAM_DDR_DQS_3_PACKAGE_LENGTH {113.63} \
    CONFIG.PCW_UIPARAM_DDR_DQS_3_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_0 {0.040} \
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_1 {0.058} \
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_2 {0.0} \
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_3 {0.0} \
    CONFIG.PCW_UIPARAM_DDR_DQ_0_LENGTH_MM {16.5} \
    CONFIG.PCW_UIPARAM_DDR_DQ_0_PACKAGE_LENGTH {98.503} \
    CONFIG.PCW_UIPARAM_DDR_DQ_0_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQ_1_LENGTH_MM {18} \
    CONFIG.PCW_UIPARAM_DDR_DQ_1_PACKAGE_LENGTH {68.5855} \
    CONFIG.PCW_UIPARAM_DDR_DQ_1_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQ_2_LENGTH_MM {0} \
    CONFIG.PCW_UIPARAM_DDR_DQ_2_PACKAGE_LENGTH {90.295} \
    CONFIG.PCW_UIPARAM_DDR_DQ_2_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DQ_3_LENGTH_MM {0} \
    CONFIG.PCW_UIPARAM_DDR_DQ_3_PACKAGE_LENGTH {103.977} \
    CONFIG.PCW_UIPARAM_DDR_DQ_3_PROPOGATION_DELAY {160} \
    CONFIG.PCW_UIPARAM_DDR_DRAM_WIDTH {16 Bits} \
    CONFIG.PCW_UIPARAM_DDR_ECC {Disabled} \
    CONFIG.PCW_UIPARAM_DDR_ENABLE {1} \
    CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ {525} \
    CONFIG.PCW_UIPARAM_DDR_HIGH_TEMP {Normal (0-85)} \
    CONFIG.PCW_UIPARAM_DDR_MEMORY_TYPE {DDR 3} \
    CONFIG.PCW_UIPARAM_DDR_PARTNO {Custom} \
    CONFIG.PCW_UIPARAM_DDR_ROW_ADDR_COUNT {15} \
    CONFIG.PCW_UIPARAM_DDR_SPEED_BIN {DDR3_1066F} \
    CONFIG.PCW_UIPARAM_DDR_TRAIN_DATA_EYE {1} \
    CONFIG.PCW_UIPARAM_DDR_TRAIN_READ_GATE {1} \
    CONFIG.PCW_UIPARAM_DDR_TRAIN_WRITE_LEVEL {1} \
    CONFIG.PCW_UIPARAM_DDR_T_FAW {40.0} \
    CONFIG.PCW_UIPARAM_DDR_T_RAS_MIN {35.0} \
    CONFIG.PCW_UIPARAM_DDR_T_RC {50.625} \
    CONFIG.PCW_UIPARAM_DDR_T_RCD {13.125} \
    CONFIG.PCW_UIPARAM_DDR_T_RP {13.125} \
    CONFIG.PCW_UIPARAM_DDR_USE_INTERNAL_VREF {0} \
    CONFIG.PCW_UIPARAM_GENERATE_SUMMARY {build/runtime/p2/ps7_aux} \
    CONFIG.PCW_USB0_BASEADDR {0xE0102000} \
    CONFIG.PCW_USB0_HIGHADDR {0xE0102fff} \
    CONFIG.PCW_USB0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_USB0_USB0_IO {MIO 28 .. 39} \
    CONFIG.PCW_USB1_PERIPHERAL_ENABLE {0} \
    CONFIG.PCW_USB_RESET_POLARITY {Active Low} \
    CONFIG.PCW_USE_AXI_FABRIC_IDLE {0} \
    CONFIG.PCW_USE_AXI_NONSECURE {0} \
    CONFIG.PCW_USE_CORESIGHT {0} \
    CONFIG.PCW_USE_CROSS_TRIGGER {0} \
    CONFIG.PCW_USE_CR_FABRIC {1} \
    CONFIG.PCW_USE_DDR_BYPASS {0} \
    CONFIG.PCW_USE_DEBUG {0} \
    CONFIG.PCW_USE_DMA0 {0} \
    CONFIG.PCW_USE_DMA1 {0} \
    CONFIG.PCW_USE_DMA2 {0} \
    CONFIG.PCW_USE_DMA3 {0} \
    CONFIG.PCW_USE_EXPANDED_IOP {0} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
    CONFIG.PCW_USE_HIGH_OCM {0} \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_M_AXI_GP1 {0} \
    CONFIG.PCW_USE_PROC_EVENT_BUS {0} \
    CONFIG.PCW_USE_PS_SLCR_REGISTERS {0} \
    CONFIG.PCW_USE_S_AXI_ACP {0} \
    CONFIG.PCW_USE_S_AXI_GP0 {0} \
    CONFIG.PCW_USE_S_AXI_GP1 {0} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} \
    CONFIG.PCW_USE_S_AXI_HP1 {1} \
    CONFIG.PCW_USE_S_AXI_HP2 {1} \
    CONFIG.PCW_USE_S_AXI_HP3 {0} \
    CONFIG.PCW_USE_TRACE {0} \
    CONFIG.PCW_VALUE_SILVERSION {3} \
    CONFIG.PCW_WDT_PERIPHERAL_CLKSRC {CPU_1X} \
    CONFIG.PCW_WDT_PERIPHERAL_DIVISOR0 {1} \
    CONFIG.PCW_WDT_PERIPHERAL_ENABLE {0} \
  ] $ps7


  # Create instance: system_reset, and set properties
  set system_reset [ create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 system_reset ]

  # Create instance: clock_locked, and set properties
  set clock_locked [ create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 clock_locked ]
  set_property -dict [list \
    CONFIG.CONST_VAL {1} \
    CONFIG.CONST_WIDTH {1} \
  ] $clock_locked


  # Create instance: processing_pipeline, and set properties
  set block_name p2_processing_bd
  set block_cell_name processing_pipeline
  if { [catch {set processing_pipeline [create_bd_cell -type module -reference $block_name $block_cell_name] } errmsg] } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2095 -severity "ERROR" "Unable to add referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   } elseif { $processing_pipeline eq "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2096 -severity "ERROR" "Unable to referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   }
    set_property -dict [list \
    CONFIG.COMPACT_POLICY_ENABLE {0} \
    CONFIG.FEATURE_DMA_ENABLE {0} \
    CONFIG.FIFO_DEPTH {1024} \
    CONFIG.FRAME_HEIGHT {480} \
    CONFIG.FRAME_WIDTH {752} \
    CONFIG.MULTI_CANDIDATE_ENABLE {0} \
    CONFIG.PROFILE_ENABLE {1} \
    CONFIG.RAW_FIFO_DEPTH {4096} \
    CONFIG.RHI_ENABLE {0} \
    CONFIG.USE_XPM {1} \
  ] $processing_pipeline


  # Create instance: control_interconnect, and set properties
  set control_interconnect [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 control_interconnect ]
  set_property -dict [list \
    CONFIG.NUM_MI {10} \
    CONFIG.NUM_SI {1} \
  ] $control_interconnect


  # Create instance: memory_interconnect, and set properties
  set memory_interconnect [ create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 memory_interconnect ]
  set_property -dict [list \
    CONFIG.NUM_MI {1} \
    CONFIG.NUM_SI {2} \
  ] $memory_interconnect


  # Create instance: interrupts, and set properties
  set interrupts [ create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 interrupts ]
  set_property CONFIG.NUM_PORTS {4} $interrupts


  # Create instance: raw_dma, and set properties
  set raw_dma [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 raw_dma ]
  set_property -dict [list \
    CONFIG.c_include_mm2s {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_include_s2mm_dre {0} \
    CONFIG.c_include_sg {0} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
    CONFIG.c_sg_length_width {20} \
  ] $raw_dma


  # Create instance: profile_dma, and set properties
  set profile_dma [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 profile_dma ]
  set_property -dict [list \
    CONFIG.c_include_mm2s {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_include_s2mm_dre {0} \
    CONFIG.c_include_sg {0} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
    CONFIG.c_sg_length_width {20} \
  ] $profile_dma


  # Create instance: video_memory_interconnect, and set properties
  set video_memory_interconnect [ create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 video_memory_interconnect ]
  set_property -dict [list \
    CONFIG.NUM_MI {1} \
    CONFIG.NUM_SI {1} \
  ] $video_memory_interconnect


  # Create instance: hdmi_system_reset, and set properties
  set hdmi_system_reset [ create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 hdmi_system_reset ]

  # Create instance: hdmi_out
  create_hier_cell_hdmi_out [current_bd_instance .] hdmi_out

  # Create instance: hdmi_vdma, and set properties
  set hdmi_vdma [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vdma:6.3 hdmi_vdma ]
  set_property -dict [list \
    CONFIG.c_include_mm2s {1} \
    CONFIG.c_include_mm2s_dre {0} \
    CONFIG.c_include_s2mm {0} \
    CONFIG.c_include_sg {0} \
    CONFIG.c_m_axi_mm2s_data_width {64} \
    CONFIG.c_m_axis_mm2s_tdata_width {32} \
    CONFIG.c_mm2s_genlock_mode {0} \
    CONFIG.c_mm2s_max_burst_length {16} \
    CONFIG.c_mm2s_sof_enable {1} \
  ] $hdmi_vdma


  # Create instance: hdmi_control_reset, and set properties
  set hdmi_control_reset [ create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 hdmi_control_reset ]

  # Create instance: motion_interconnect, and set properties
  set motion_interconnect [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 motion_interconnect ]
  set_property -dict [list \
    CONFIG.NUM_MI {2} \
    CONFIG.NUM_SI {1} \
  ] $motion_interconnect


  # Create instance: motor_controller, and set properties
  set block_name motor_control_bd
  set block_cell_name motor_controller
  if { [catch {set motor_controller [create_bd_cell -type module -reference $block_name $block_cell_name] } errmsg] } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2095 -severity "ERROR" "Unable to add referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   } elseif { $motor_controller eq "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2096 -severity "ERROR" "Unable to referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   }
    set_property CONFIG.CLOCK_HZ {100000000} $motor_controller


  # Create instance: bsg1_guard, and set properties
  set block_name b_motion_guard_bd
  set block_cell_name bsg1_guard
  if { [catch {set bsg1_guard [create_bd_cell -type module -reference $block_name $block_cell_name] } errmsg] } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2095 -severity "ERROR" "Unable to add referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   } elseif { $bsg1_guard eq "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2096 -severity "ERROR" "Unable to referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   }
    set_property -dict [list \
    CONFIG.CARRIER_MODE {0} \
    CONFIG.CLOCK_HZ {100000000} \
    CONFIG.HEARTBEAT_HALF_CYCLES {500000} \
  ] $bsg1_guard


  # Create instance: bsg1_field_power_good, and set properties
  set bsg1_field_power_good [ create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 bsg1_field_power_good ]
  set_property -dict [list \
    CONFIG.CONST_VAL {1} \
    CONFIG.CONST_WIDTH {1} \
  ] $bsg1_field_power_good


  # Create instance: pfo3_frontend, and set properties
  set block_name pfo3_axi_bd
  set block_cell_name pfo3_frontend
  if { [catch {set pfo3_frontend [create_bd_cell -type module -reference $block_name $block_cell_name] } errmsg] } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2095 -severity "ERROR" "Unable to add referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   } elseif { $pfo3_frontend eq "" } {
     catch {common::send_gid_msg -ssname BD::TCL -id 2096 -severity "ERROR" "Unable to referenced block <$block_name>. Please add the files for ${block_name}'s definition into the project."}
     return 1
   }
  
  # Create instance: pfo3_dma, and set properties
  set pfo3_dma [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 pfo3_dma ]
  set_property -dict [list \
    CONFIG.c_include_mm2s {1} \
    CONFIG.c_include_mm2s_dre {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_include_s2mm_dre {0} \
    CONFIG.c_include_sg {1} \
    CONFIG.c_m_axi_mm2s_data_width {64} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_m_axis_mm2s_tdata_width {32} \
    CONFIG.c_mm2s_burst_size {16} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_sg_length_width {24} \
  ] $pfo3_dma


  # Create instance: pfo3_memory, and set properties
  set pfo3_memory [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 pfo3_memory ]
  set_property -dict [list \
    CONFIG.NUM_MI {1} \
    CONFIG.NUM_SI {3} \
  ] $pfo3_memory


  # Create interface connections
  connect_bd_intf_net -intf_net control_interconnect_M00_AXI [get_bd_intf_pins control_interconnect/M00_AXI] [get_bd_intf_pins processing_pipeline/S_AXI_CONTROL]
  connect_bd_intf_net -intf_net control_interconnect_M01_AXI [get_bd_intf_pins control_interconnect/M01_AXI] [get_bd_intf_pins processing_pipeline/S_AXI_DIAG]
  connect_bd_intf_net -intf_net control_interconnect_M02_AXI [get_bd_intf_pins control_interconnect/M02_AXI] [get_bd_intf_pins raw_dma/S_AXI_LITE]
  connect_bd_intf_net -intf_net control_interconnect_M03_AXI [get_bd_intf_pins control_interconnect/M03_AXI] [get_bd_intf_pins profile_dma/S_AXI_LITE]
  connect_bd_intf_net -intf_net control_interconnect_M04_AXI [get_bd_intf_pins control_interconnect/M04_AXI] [get_bd_intf_pins hdmi_vdma/S_AXI_LITE]
  connect_bd_intf_net -intf_net control_interconnect_M05_AXI [get_bd_intf_pins control_interconnect/M05_AXI] [get_bd_intf_pins hdmi_out/S_AXI_VTC]
  connect_bd_intf_net -intf_net control_interconnect_M06_AXI [get_bd_intf_pins control_interconnect/M06_AXI] [get_bd_intf_pins hdmi_out/S_AXI_DYNCLK]
  connect_bd_intf_net -intf_net control_interconnect_M07_AXI [get_bd_intf_pins control_interconnect/M07_AXI] [get_bd_intf_pins motion_interconnect/S00_AXI]
  connect_bd_intf_net -intf_net control_interconnect_M08_AXI [get_bd_intf_pins control_interconnect/M08_AXI] [get_bd_intf_pins pfo3_frontend/S_AXI]
  connect_bd_intf_net -intf_net control_interconnect_M09_AXI [get_bd_intf_pins control_interconnect/M09_AXI] [get_bd_intf_pins pfo3_dma/S_AXI_LITE]
  connect_bd_intf_net -intf_net hdmi_out_TMDS [get_bd_intf_ports hdmi_out] [get_bd_intf_pins hdmi_out/TMDS]
  connect_bd_intf_net -intf_net hdmi_vdma_M_AXIS_MM2S [get_bd_intf_pins hdmi_vdma/M_AXIS_MM2S] [get_bd_intf_pins hdmi_out/in_stream]
  connect_bd_intf_net -intf_net hdmi_vdma_M_AXI_MM2S [get_bd_intf_pins hdmi_vdma/M_AXI_MM2S] [get_bd_intf_pins video_memory_interconnect/S00_AXI]
  connect_bd_intf_net -intf_net memory_interconnect_M00_AXI [get_bd_intf_pins memory_interconnect/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP0]
  connect_bd_intf_net -intf_net motion_interconnect_M00_AXI [get_bd_intf_pins motion_interconnect/M00_AXI] [get_bd_intf_pins motor_controller/S_AXI]
  connect_bd_intf_net -intf_net motion_interconnect_M01_AXI [get_bd_intf_pins motion_interconnect/M01_AXI] [get_bd_intf_pins bsg1_guard/S_AXI]
  connect_bd_intf_net -intf_net pfo3_dma_M_AXIS_MM2S [get_bd_intf_pins pfo3_dma/M_AXIS_MM2S] [get_bd_intf_pins pfo3_frontend/S_AXIS]
  connect_bd_intf_net -intf_net pfo3_dma_M_AXI_MM2S [get_bd_intf_pins pfo3_dma/M_AXI_MM2S] [get_bd_intf_pins pfo3_memory/S00_AXI]
  connect_bd_intf_net -intf_net pfo3_dma_M_AXI_S2MM [get_bd_intf_pins pfo3_dma/M_AXI_S2MM] [get_bd_intf_pins pfo3_memory/S01_AXI]
  connect_bd_intf_net -intf_net pfo3_dma_M_AXI_SG [get_bd_intf_pins pfo3_dma/M_AXI_SG] [get_bd_intf_pins pfo3_memory/S02_AXI]
  connect_bd_intf_net -intf_net pfo3_frontend_M_AXIS [get_bd_intf_pins pfo3_frontend/M_AXIS] [get_bd_intf_pins pfo3_dma/S_AXIS_S2MM]
  connect_bd_intf_net -intf_net pfo3_memory_M00_AXI [get_bd_intf_pins pfo3_memory/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP2]
  connect_bd_intf_net -intf_net processing_pipeline_M_AXIS_PROFILE [get_bd_intf_pins processing_pipeline/M_AXIS_PROFILE] [get_bd_intf_pins profile_dma/S_AXIS_S2MM]
  connect_bd_intf_net -intf_net processing_pipeline_M_AXIS_RAW [get_bd_intf_pins processing_pipeline/M_AXIS_RAW] [get_bd_intf_pins raw_dma/S_AXIS_S2MM]
  connect_bd_intf_net -intf_net profile_dma_M_AXI_S2MM [get_bd_intf_pins profile_dma/M_AXI_S2MM] [get_bd_intf_pins memory_interconnect/S01_AXI]
  connect_bd_intf_net -intf_net ps7_DDR [get_bd_intf_ports DDR] [get_bd_intf_pins ps7/DDR]
  connect_bd_intf_net -intf_net ps7_FIXED_IO [get_bd_intf_ports FIXED_IO] [get_bd_intf_pins ps7/FIXED_IO]
  connect_bd_intf_net -intf_net ps7_M_AXI_GP0 [get_bd_intf_pins ps7/M_AXI_GP0] [get_bd_intf_pins control_interconnect/S00_AXI]
  connect_bd_intf_net -intf_net raw_dma_M_AXI_S2MM [get_bd_intf_pins raw_dma/M_AXI_S2MM] [get_bd_intf_pins memory_interconnect/S00_AXI]
  connect_bd_intf_net -intf_net video_memory_interconnect_M00_AXI [get_bd_intf_pins video_memory_interconnect/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP1]

  # Create port connections
  connect_bd_net -net bsg1_field_power_good_dout  [get_bd_pins bsg1_field_power_good/dout] \
  [get_bd_pins bsg1_guard/field_power_good]
  connect_bd_net -net bsg1_guard_direction  [get_bd_pins bsg1_guard/direction] \
  [get_bd_ports motor_dir]
  connect_bd_net -net bsg1_guard_enable_n  [get_bd_pins bsg1_guard/enable_n] \
  [get_bd_ports motor_enable]
  connect_bd_net -net bsg1_guard_heartbeat  [get_bd_pins bsg1_guard/heartbeat] \
  [get_bd_ports motor_heartbeat]
  connect_bd_net -net bsg1_guard_step  [get_bd_pins bsg1_guard/step] \
  [get_bd_ports motor_step]
  connect_bd_net -net cam_data_0_1  [get_bd_ports cam_data] \
  [get_bd_pins processing_pipeline/cam_data]
  connect_bd_net -net cam_fv_0_1  [get_bd_ports cam_fv] \
  [get_bd_pins processing_pipeline/cam_fv]
  connect_bd_net -net cam_lv_0_1  [get_bd_ports cam_lv] \
  [get_bd_pins processing_pipeline/cam_lv]
  connect_bd_net -net cam_pclk_0_1  [get_bd_ports cam_pclk] \
  [get_bd_pins processing_pipeline/cam_pclk]
  connect_bd_net -net cam_scl_i_0_1  [get_bd_ports cam_scl_i] \
  [get_bd_pins processing_pipeline/cam_scl_i]
  connect_bd_net -net cam_sda_i_0_1  [get_bd_ports cam_sda_i] \
  [get_bd_pins processing_pipeline/cam_sda_i]
  connect_bd_net -net clock_locked_dout  [get_bd_pins clock_locked/dout] \
  [get_bd_pins system_reset/dcm_locked]
  connect_bd_net -net hdmi_control_reset_peripheral_aresetn  [get_bd_pins hdmi_control_reset/peripheral_aresetn] \
  [get_bd_pins hdmi_vdma/axi_resetn]
  connect_bd_net -net hdmi_out_LOCKED_O  [get_bd_pins hdmi_out/LOCKED_O] \
  [get_bd_pins hdmi_system_reset/dcm_locked] \
  [get_bd_pins hdmi_control_reset/dcm_locked]
  connect_bd_net -net hdmi_out_hdmi_out_hpd  [get_bd_pins hdmi_out/hdmi_out_hpd] \
  [get_bd_ports hdmi_out_hpd]
  connect_bd_net -net hdmi_system_reset_peripheral_aresetn  [get_bd_pins hdmi_system_reset/peripheral_aresetn] \
  [get_bd_pins video_memory_interconnect/aresetn] \
  [get_bd_pins hdmi_out/resetn_142M]
  connect_bd_net -net hdmi_vdma_mm2s_introut  [get_bd_pins hdmi_vdma/mm2s_introut] \
  [get_bd_pins interrupts/In3]
  connect_bd_net -net interrupts_dout  [get_bd_pins interrupts/dout] \
  [get_bd_pins ps7/IRQ_F2P]
  connect_bd_net -net motor_controller_direction  [get_bd_pins motor_controller/direction] \
  [get_bd_pins bsg1_guard/motor_direction]
  connect_bd_net -net motor_controller_motor_busy  [get_bd_pins motor_controller/motor_busy] \
  [get_bd_pins bsg1_guard/motor_busy]
  connect_bd_net -net motor_controller_motor_fault  [get_bd_pins motor_controller/motor_fault] \
  [get_bd_pins bsg1_guard/motor_fault]
  connect_bd_net -net motor_controller_step  [get_bd_pins motor_controller/step] \
  [get_bd_pins bsg1_guard/motor_step]
  connect_bd_net -net motor_controller_telemetry_accepted_id  [get_bd_pins motor_controller/telemetry_accepted_id] \
  [get_bd_pins processing_pipeline/motor_accepted_id]
  connect_bd_net -net motor_controller_telemetry_emitted  [get_bd_pins motor_controller/telemetry_emitted] \
  [get_bd_pins processing_pipeline/motor_emitted]
  connect_bd_net -net motor_controller_telemetry_epoch  [get_bd_pins motor_controller/telemetry_epoch] \
  [get_bd_pins processing_pipeline/motor_epoch]
  connect_bd_net -net processing_pipeline_cam_reset_n  [get_bd_pins processing_pipeline/cam_reset_n] \
  [get_bd_ports cam_reset_n]
  connect_bd_net -net processing_pipeline_cam_scl_o  [get_bd_pins processing_pipeline/cam_scl_o] \
  [get_bd_ports cam_scl_o]
  connect_bd_net -net processing_pipeline_cam_scl_t  [get_bd_pins processing_pipeline/cam_scl_t] \
  [get_bd_ports cam_scl_t]
  connect_bd_net -net processing_pipeline_cam_sda_o  [get_bd_pins processing_pipeline/cam_sda_o] \
  [get_bd_ports cam_sda_o]
  connect_bd_net -net processing_pipeline_cam_sda_t  [get_bd_pins processing_pipeline/cam_sda_t] \
  [get_bd_ports cam_sda_t]
  connect_bd_net -net processing_pipeline_cam_standby  [get_bd_pins processing_pipeline/cam_standby] \
  [get_bd_ports cam_standby]
  connect_bd_net -net processing_pipeline_irq  [get_bd_pins processing_pipeline/irq] \
  [get_bd_pins interrupts/In0]
  connect_bd_net -net profile_dma_s2mm_introut  [get_bd_pins profile_dma/s2mm_introut] \
  [get_bd_pins interrupts/In2]
  connect_bd_net -net ps7_FCLK_CLK0  [get_bd_pins ps7/FCLK_CLK0] \
  [get_bd_pins ps7/M_AXI_GP0_ACLK] \
  [get_bd_pins ps7/S_AXI_HP0_ACLK] \
  [get_bd_pins system_reset/slowest_sync_clk] \
  [get_bd_pins memory_interconnect/aclk] \
  [get_bd_pins control_interconnect/ACLK] \
  [get_bd_pins control_interconnect/S00_ACLK] \
  [get_bd_pins control_interconnect/M00_ACLK] \
  [get_bd_pins control_interconnect/M01_ACLK] \
  [get_bd_pins control_interconnect/M02_ACLK] \
  [get_bd_pins control_interconnect/M03_ACLK] \
  [get_bd_pins control_interconnect/M04_ACLK] \
  [get_bd_pins control_interconnect/M05_ACLK] \
  [get_bd_pins control_interconnect/M06_ACLK] \
  [get_bd_pins raw_dma/s_axi_lite_aclk] \
  [get_bd_pins raw_dma/m_axi_s2mm_aclk] \
  [get_bd_pins profile_dma/s_axi_lite_aclk] \
  [get_bd_pins profile_dma/m_axi_s2mm_aclk] \
  [get_bd_pins hdmi_vdma/s_axi_lite_aclk] \
  [get_bd_pins hdmi_out/clk_100M] \
  [get_bd_pins hdmi_control_reset/slowest_sync_clk] \
  [get_bd_pins motor_controller/clk] \
  [get_bd_pins control_interconnect/M07_ACLK] \
  [get_bd_pins motion_interconnect/ACLK] \
  [get_bd_pins motion_interconnect/S00_ACLK] \
  [get_bd_pins motion_interconnect/M00_ACLK] \
  [get_bd_pins motion_interconnect/M01_ACLK] \
  [get_bd_pins processing_pipeline/system_clk] \
  [get_bd_pins processing_pipeline/s_axi_control_aclk] \
  [get_bd_pins processing_pipeline/s_axi_diag_aclk] \
  [get_bd_pins pfo3_dma/s_axi_lite_aclk] \
  [get_bd_pins pfo3_dma/m_axi_mm2s_aclk] \
  [get_bd_pins pfo3_dma/m_axi_s2mm_aclk] \
  [get_bd_pins pfo3_dma/m_axi_sg_aclk] \
  [get_bd_pins pfo3_memory/ACLK] \
  [get_bd_pins pfo3_memory/S00_ACLK] \
  [get_bd_pins pfo3_memory/S01_ACLK] \
  [get_bd_pins pfo3_memory/S02_ACLK] \
  [get_bd_pins pfo3_memory/M00_ACLK] \
  [get_bd_pins control_interconnect/M08_ACLK] \
  [get_bd_pins control_interconnect/M09_ACLK] \
  [get_bd_pins ps7/S_AXI_HP2_ACLK] \
  [get_bd_pins bsg1_guard/clk] \
  [get_bd_pins pfo3_frontend/aclk]
  connect_bd_net -net ps7_FCLK_CLK1  [get_bd_pins ps7/FCLK_CLK1] \
  [get_bd_pins ps7/S_AXI_HP1_ACLK] \
  [get_bd_pins video_memory_interconnect/aclk] \
  [get_bd_pins hdmi_system_reset/slowest_sync_clk] \
  [get_bd_pins hdmi_vdma/m_axi_mm2s_aclk] \
  [get_bd_pins hdmi_vdma/m_axis_mm2s_aclk] \
  [get_bd_pins hdmi_out/clk_142M]
  connect_bd_net -net ps7_FCLK_RESET0_N  [get_bd_pins ps7/FCLK_RESET0_N] \
  [get_bd_pins system_reset/ext_reset_in] \
  [get_bd_pins hdmi_system_reset/ext_reset_in] \
  [get_bd_pins hdmi_control_reset/ext_reset_in]
  connect_bd_net -net raw_dma_s2mm_introut  [get_bd_pins raw_dma/s2mm_introut] \
  [get_bd_pins interrupts/In1]
  connect_bd_net -net system_reset_interconnect_aresetn  [get_bd_pins system_reset/interconnect_aresetn] \
  [get_bd_pins control_interconnect/ARESETN] \
  [get_bd_pins control_interconnect/S00_ARESETN] \
  [get_bd_pins control_interconnect/M00_ARESETN] \
  [get_bd_pins control_interconnect/M01_ARESETN] \
  [get_bd_pins control_interconnect/M02_ARESETN] \
  [get_bd_pins control_interconnect/M03_ARESETN] \
  [get_bd_pins control_interconnect/M04_ARESETN] \
  [get_bd_pins control_interconnect/M05_ARESETN] \
  [get_bd_pins control_interconnect/M06_ARESETN] \
  [get_bd_pins control_interconnect/M07_ARESETN] \
  [get_bd_pins motion_interconnect/ARESETN] \
  [get_bd_pins motion_interconnect/S00_ARESETN] \
  [get_bd_pins motion_interconnect/M00_ARESETN] \
  [get_bd_pins motion_interconnect/M01_ARESETN]
  connect_bd_net -net system_reset_peripheral_aresetn  [get_bd_pins system_reset/peripheral_aresetn] \
  [get_bd_pins memory_interconnect/aresetn] \
  [get_bd_pins raw_dma/axi_resetn] \
  [get_bd_pins profile_dma/axi_resetn] \
  [get_bd_pins hdmi_out/resetn_100M] \
  [get_bd_pins motor_controller/resetn] \
  [get_bd_pins processing_pipeline/system_resetn] \
  [get_bd_pins processing_pipeline/s_axi_control_aresetn] \
  [get_bd_pins processing_pipeline/s_axi_diag_aresetn] \
  [get_bd_pins pfo3_dma/axi_resetn] \
  [get_bd_pins pfo3_memory/ARESETN] \
  [get_bd_pins pfo3_memory/S00_ARESETN] \
  [get_bd_pins pfo3_memory/S01_ARESETN] \
  [get_bd_pins pfo3_memory/S02_ARESETN] \
  [get_bd_pins pfo3_memory/M00_ARESETN] \
  [get_bd_pins control_interconnect/M08_ARESETN] \
  [get_bd_pins control_interconnect/M09_ARESETN] \
  [get_bd_pins bsg1_guard/resetn] \
  [get_bd_pins pfo3_frontend/aresetn]

  # Create address segments
  assign_bd_address -offset 0x43C30000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs hdmi_out/axi_dynclk/s00_axi/reg0] -force
  assign_bd_address -offset 0x43C50000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs bsg1_guard/S_AXI/reg0] -force
  assign_bd_address -offset 0x40420000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs hdmi_vdma/S_AXI_LITE/Reg] -force
  assign_bd_address -offset 0x43C40000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs motor_controller/S_AXI/reg0] -force
  assign_bd_address -offset 0x40430000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs pfo3_dma/S_AXI_LITE/Reg] -force
  assign_bd_address -offset 0x40000000 -range 0x00001000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs pfo3_frontend/S_AXI/reg0] -force
  assign_bd_address -offset 0x43C10000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs processing_pipeline/S_AXI_DIAG/reg0] -force
  assign_bd_address -offset 0x43C00000 -range 0x00010000 -with_name SEG_processing_pipeline_reg0_1 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs processing_pipeline/S_AXI_CONTROL/reg0] -force
  assign_bd_address -offset 0x40410000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs profile_dma/S_AXI_LITE/Reg] -force
  assign_bd_address -offset 0x40400000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs raw_dma/S_AXI_LITE/Reg] -force
  assign_bd_address -offset 0x43C20000 -range 0x00010000 -target_address_space [get_bd_addr_spaces ps7/Data] [get_bd_addr_segs hdmi_out/vtc_out/ctrl/Reg] -force
  assign_bd_address -offset 0x00000000 -range 0x20000000 -target_address_space [get_bd_addr_spaces raw_dma/Data_S2MM] [get_bd_addr_segs ps7/S_AXI_HP0/HP0_DDR_LOWOCM] -force
  assign_bd_address -offset 0x00000000 -range 0x20000000 -target_address_space [get_bd_addr_spaces profile_dma/Data_S2MM] [get_bd_addr_segs ps7/S_AXI_HP0/HP0_DDR_LOWOCM] -force
  assign_bd_address -offset 0x00000000 -range 0x20000000 -target_address_space [get_bd_addr_spaces hdmi_vdma/Data_MM2S] [get_bd_addr_segs ps7/S_AXI_HP1/HP1_DDR_LOWOCM] -force
  assign_bd_address -offset 0x00000000 -range 0x20000000 -target_address_space [get_bd_addr_spaces pfo3_dma/Data_MM2S] [get_bd_addr_segs ps7/S_AXI_HP2/HP2_DDR_LOWOCM] -force
  assign_bd_address -offset 0x00000000 -range 0x20000000 -target_address_space [get_bd_addr_spaces pfo3_dma/Data_S2MM] [get_bd_addr_segs ps7/S_AXI_HP2/HP2_DDR_LOWOCM] -force
  assign_bd_address -offset 0x00000000 -range 0x20000000 -target_address_space [get_bd_addr_spaces pfo3_dma/Data_SG] [get_bd_addr_segs ps7/S_AXI_HP2/HP2_DDR_LOWOCM] -force


  # Restore current instance
  current_bd_instance $oldCurInst

  validate_bd_design
  save_bd_design
}
# End of create_root_design()


##################################################################
# MAIN FLOW
##################################################################

create_root_design ""


