# Bitstream from the gated, routed seed-retry checkpoint (the in-flow gate failed only on the
# lane-name regex, fixed in review_build_r43.py; the review is re-run here and must pass).
set root [file normalize [file join [file dirname [info script]] ..]]
set build [file join $root build seedretry]
set py [auto_execok python]
if {$py eq ""} {error "Python required"}
puts [exec {*}$py -B [file join $root scripts review_build_final.py] --reports [file join $build reports] --output [file join $build ADMISSION_FINAL.json]]
open_checkpoint [file join $build artifacts route.dcp]
set_param general.maxThreads 2
write_bitstream -bin_file [file join $build artifacts final.bit]
puts "SEEDRETRY_BIT_COMPLETE_FROM_GATED_ROUTED_DCP"
