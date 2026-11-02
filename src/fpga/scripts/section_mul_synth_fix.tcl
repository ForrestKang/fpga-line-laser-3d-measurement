set_param general.maxThreads 2
read_verilog -sv rtl/section_measure.sv
synth_design -top sm_mul -part xc7z020clg400-1 -mode out_of_context -flatten_hierarchy rebuilt
report_utilization -file build/section_mul_dsp_add01.rpt
