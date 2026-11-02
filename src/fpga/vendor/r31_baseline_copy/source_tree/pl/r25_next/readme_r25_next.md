# Isolated four-lane adjacency candidate

This directory is an experimental successor to the frozen native V6 in
`../r25`. It is absent from the official integration source list and was not
used to generate the delivered bitstream. The module names and native ports are
kept unchanged so the existing native test contract remains applicable; compile
this directory independently, never together with `../r25/rtl`.

The frozen candidate is V9. Its independent native OOC implementation passed
the actual 10 ns clock: routed WNS +0.275 ns, WHS +0.016 ns and WPWS +3.750 ns,
with no setup, hold or pulse-width failing endpoints. Routed utilization is
16182 LUT, 9241 FF, 39.5 BRAM tiles and one DSP, with zero latches. These results
come from `build/synth_v9` and `TIMING_R25.json`, bound by `FREEZE_V9.json`.
External native I/O timing is unconstrained in OOC; the result does not close
the integrated wrapper or authorize adopting this candidate for a board run.

The change is restricted to the adjacent-row 16 by 16 comparison. Compact
descriptors and predecessor-count, predecessor-ID, ambiguity and stability
state are interleaved by slot modulo four into four dual-port BRAM banks. A
group processes one source node and four aligned target slots. Four independent
target updates use the four B ports. Source successor counts use a popcount,
source ambiguity uses OR, and source stability uses a maximum before a single
A-port write. Source and target are in different rows, including when their
bank indices coincide, so pair processing never writes the same address from
both ports.

The three algorithm passes remain count, simultaneous ambiguity marking and
stability over surviving edges. Label propagation, canonical branch IDs,
support counts, measurement/preview policies, reasons, overflow handling, all
pixel extraction and output records retain the V6 integer contract. V8 inserted
two registered, balanced maximum-reduction stages only in the stability pass.
V9 additionally registers the descriptor operands and eligibility before the
four subtract/threshold lanes. Each pass takes one additional cycle per group.
This is a four-lane comparator and update datapath with one group every five
or seven clocks; it does not accept a new four-edge group on every clock.
For a 480-row frame the expected graph phase is therefore
`479 rows * 16 sources * 4 target groups * (5+5+7) = 521152 cycles`.
The testbench observes this count on every full-size frame.

All 17 full-size 752 by 480 frames passed the independent integer reference
(986026 candidate-word checks and 8160 rows), and all 17 complete record streams
have exactly the same SHA256 as the frozen V6 outputs. The 24 descriptor-level
trials passed 52224 additional word checks. Native frame cycles range from
2805946 to 3311721, or 28.05946 to 33.11721 ms at the OOC-closed 100 MHz clock.
These counts include the native testbench's seeded valid/ready blockage, not
AXI/DDR/DMA/software latency. The frozen V6 worst case was 4262027 cycles; the
corresponding V9 case saved 950306 cycles. No integrated performance claim is
made from that difference.

`scripts/regression.ps1` compiles once through Vivado-generated XSim scripts and
runs the same 17 full-size saved/synthetic frames plus SOF and STOP checks.
`scripts/faults.ps1` additionally checks STOP while a diagnostic is held under
backpressure, normal restart, early/missing EOF, reset/restart and STOP in each
new reduction/compare state followed by normal restart. Both read the
frozen `../r25/vectors` directory. They never regenerate or modify those inputs.
The copied vector helper rejects its generation commands before any write.
`scripts/report.py` compares all fields to the independent integer reference;
`scripts/metrics.py` also requires every complete emitted record stream to have
the same SHA256 as frozen V6 and binds all source manifests.

`scripts/graph_equivalence.ps1` runs 24 descriptor-level trials against a copied
V6 top with only its module name changed. It intentionally constructs four
simultaneous matches, ambiguous fans, unique chains, width and distance
rejections, weak measurement peaks, displacement maxima and invalid-capacity
rows. Some dense descriptors need not be physically realizable from one pixel
row; they exercise RAM and reduction behavior beyond the image fixtures.

Phase, native input valid-without-ready and native output valid-without-ready
counts are testbench observations saved in `results/*.phases`. They are not
new hardware ABI fields. This native engine has no DDR interface and cannot
truthfully provide DDR stall, AXI burst or DMA FIFO high-water counters. No
DDR counter is inferred from native blockage. Three-row direction prediction
and an independent second-order residual gate remain outside this change.

`BUILD_PIN_V9.json` pins the RTL and actual OOC Tcl before synthesis.
`SOURCE_MANIFEST.json`, `VALIDATION_R25.json`, `FOUR_LANE_METRICS.json` and
`TIMING_R25.json` are the checked evidence products. The OOC build constrains
only the native 10 ns clock; wrapper I/O, integrated CDC, complete-device timing
and actual DDR performance require a separate integration review.
The OOC DRC has three warnings: DSP input pipelining, unconnected OOC port loads,
and absence of PS7 in a native-only top. It has no Error or Critical Warning.
The small positive hold margin must be rechecked in the full integrated design.

V7 passed the image and descriptor equivalence tests but failed 100 MHz timing:
the serial four-input maximum drove a 26-level BRAM-to-write-enable path.
Synthesized WNS was -11.891 ns and routed WNS was -10.506 ns. Its exact sources,
tests, outputs and reports are retained under `revisions/v7` and
`build/synth_v7`; `revisions/v7/ARCHIVE.json` maps historical compile paths to
the preserved source bytes. V8 then passed synthesis timing (+0.226 ns) but
failed route at -0.259 ns in the descriptor RAM to threshold-comparison path.
Its 17 image frames, 24 graph trials, abnormal/restart tests and exact source
snapshot remain under `revisions/v8`, with DCP/reports under `build/synth_v8`.
V9 does not erase either failure.
