# Resume after the one corrected module-reference refresh, preserving live BD.
set f [open D:/Workspace/CodexWork/scan3d_pixel_frontend_20261021_25/pl/integration/prepare_bd_r25.tcl r]
set lines [split [read $f] "\n"]
close $f
set found [lsearch -glob $lines {set_property -dict *CONFIG.COMPACT_POLICY_ENABLE*}]
if {$found<0} {error resume_start_not_found}
eval [join [lrange $lines $found end] "\n"]
