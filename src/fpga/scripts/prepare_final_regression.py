"""Create derived full-size integration tests without rewriting historical tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = Path("D:/Workspace/CodexWork/scan3d_r42_buttons_boot_20261029_01")


def main():
    tb = (OLD / "tests/integration_axi_tb.sv").read_text()
    tb = tb.replace("module integration_axi_tb;", "module integration_axi_tb;\n reg [4095:0] section_path;\n reg [31:0] section_config[0:14];\n integer sc, section_attempts;\n reg section_test=0;")
    marker = "  write_register(12'h010,1);\n  for(j=0;j<W*H/4;j=j+1)begin"
    assert tb.count(marker) == 1
    tb = tb.replace(marker, """  section_test=$value$plusargs("SECTION_CONFIG=%s",section_path);
  if(section_test)begin
   $readmemh(section_path,section_config);
   read_register(12'h700,read_value);
   if(read_value!==32'h5345434d)$fatal(1,"section magic %h",read_value);
   for(sc=0;sc<15;sc=sc+1)write_register(12'h708+sc*4,section_config[sc]);
   write_register(12'h704,3);
  end
""" + marker)
    marker = '  $display("TEST_PASS words=%0d cycles=%0d geo=%0d moments=%0d",words,cycle,dut.geo_record_count,dut.moment_accepted);'
    assert tb.count(marker) == 1
    tb = tb.replace(marker, """  if(section_test)begin
   section_attempts=0;read_register(12'h750,read_value);
   while(read_value!=1&&section_attempts<2000000)begin
    read_register(12'h750,read_value);section_attempts=section_attempts+1;
   end
   if(read_value!=1)$fatal(1,"section solve deadline");
   $write("SECTION_R");
   for(sc=8'h50;sc<=8'h7c;sc=sc+4)begin read_register(12'h700+sc,read_value);$write(" %08x",read_value);end
   read_register(12'h79c,read_value);$write(" %08x",read_value);
   read_register(12'h7a0,read_value);$write(" %08x",read_value);
   read_register(12'h7a4,read_value);$display(" %08x",read_value);
   for(sc=0;sc<16;sc=sc+1)begin
    write_register(12'h744,sc|(sc<<8));$write("SECTION_T %0d",sc);
    for(hk=8'h80;hk<=8'h98;hk=hk+4)begin read_register(12'h700+hk,read_value);$write(" %08x",read_value);end
    $display("");
   end
  end
""" + marker)
    dest = ROOT / "tests/integration_axi_final_tb.sv"
    with dest.open("x", encoding="utf-8") as f:
        f.write(tb)
    text = (ROOT / "scripts/integration_verify_r40.py").read_text()
    text = text.replace("VERIFY = ROOT / 'vendor/verification'", f"VERIFY = Path({str(OLD / 'vendor/verification')!r})")
    text = text.replace("ROOT / 'runtime12'", "ROOT / 'runtime'")
    text = text.replace("ROOT / 'tests/xpm_stubs_sim.v'", f"Path({str(OLD / 'tests/xpm_stubs_sim.v')!r})")
    text = text.replace("ROOT / 'tests/integration_axi_tb.sv'", "ROOT / 'tests/integration_axi_final_tb.sv'")
    text = text.replace("ROOT / 'rtl/r41_measure.sv']", "ROOT / 'rtl/r41_measure.sv', ROOT / 'rtl/section_measure.sv']")
    # Optional section mode enables the actual AXI-connected snoop for each input.
    text = text.replace("for case_name in cases:", "section_mode = os.environ.get('FINAL_SECTION_TEST') == '1'\nfor case_name in cases:")
    needle = "    run([IVER / 'vvp.exe', OUT / 'sim.vvp', '+INPUT=' + str(input_hex), '+CONFIG=' + str(cfg),\n         '+HEADER=' + str(hdr), '+OUTPUT=' + str(stream)], case + '.log')"
    assert text.count(needle) == 1
    text = text.replace(needle, """    extra = []
    if section_mode:
        sys.path.insert(0, str(ROOT / 'model'))
        import section_measure_model as section_model
        # A deterministic laser-plane basis is written through the AXI window.
        section_cfg = section_model.Config(origin=(0, 0, 0), es=(1<<24, 0, 0), eh=(0, 1<<24, 0))
        section_values = list(section_cfg.origin + section_cfg.es + section_cfg.eh) + [section_cfg.z_min, section_cfg.z_max, section_cfg.jump, section_cfg.brk, section_cfg.level, section_cfg.min_points]
        section_path = OUT / (case + '_section.hex')
        section_path.write_text('\\n'.join(f'{v & 0xffffffff:08x}' for v in section_values) + '\\n')
        extra = ['+SECTION_CONFIG=' + str(section_path)]
    run([IVER / 'vvp.exe', OUT / 'sim.vvp', '+INPUT=' + str(input_hex), '+CONFIG=' + str(cfg),
         '+HEADER=' + str(hdr), '+OUTPUT=' + str(stream)] + extra, case + '.log')""")
    text = text.replace("if not binned and case not in LIVE:", "if not section_mode and not binned and case not in LIVE:")
    needle = "    old_patches = old_ref.graph_patches(records_b if binned else records, rows)"
    assert text.count(needle) == 1
    text = text.replace(needle, """    section_result = None
    if section_mode:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from section_rtl_verify import encode
        expected_section = section_model.SectionMeasure(section_cfg).packet(actual_geo)
        expected_encoded = encode(expected_section, 1, expected_section['dropped'])
        section_line = next(l for l in log.splitlines() if l.startswith('SECTION_R')).split()[1:]
        section_regs = [int(x, 16) for x in section_line]
        assert section_regs == expected_encoded['regs'], (case, 'section regs', section_regs, expected_encoded['regs'])
        section_tables = [[int(x,16) for x in l.split()[2:]] for l in log.splitlines() if l.startswith('SECTION_T')]
        for index, values in enumerate(expected_encoded['steps']):
            assert section_tables[index][:2] == list(values), (case, 'section step', index)
        for index, values in enumerate(expected_encoded['seg']):
            assert section_tables[index][2:] == list(values), (case, 'section segment', index)
        section_result = dict(registers_exact=True, tables_exact=True, points=expected_section['points'], segments=expected_section['segments'])
""" + needle)
    text = text.replace("processing_cycles_r40_r39=[words[-32 + 14], r39_cycles], view_ingest=view, measure=measure,", "processing_cycles_r40_r39=[words[-32 + 14], r39_cycles], view_ingest=view, measure=measure, section=section_result,")
    # R39 comparison legitimately changes its cycle counters in section-mode only.
    dest = ROOT / "scripts/integration_verify_final.py"
    with dest.open("x", encoding="utf-8") as f:
        f.write(text)
    print(dest)


if __name__ == "__main__":
    main()
