"""One-time preservation and explicit wiring of the final section register window."""
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    out = ROOT / "backups" / ("integration_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=False)
    originals = ["rtl/r35_axi.sv", "scripts/create_project_r43.tcl"]
    inventory = []
    for name in originals:
        p = ROOT / name
        dest = out / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        assert digest == hashlib.sha256(dest.read_bytes()).hexdigest()
        inventory.append(dict(path=name, bytes=p.stat().st_size, sha256=digest))
    (out / "MANIFEST.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    p = ROOT / originals[0]
    t = p.read_text()
    assert "section_measure section" not in t
    t = t.replace("reg [31:0] wdata;", '(* max_fanout = 32 *) reg [31:0] wdata;')
    t = t.replace("reg meas_we;wire [31:0] meas_rdata;", "reg meas_we;wire [31:0] meas_rdata;\n reg sec_we;wire [31:0] sec_rdata;")
    t = t.replace("meas_we<=0;", "meas_we<=0;sec_we<=0;")
    t = t.replace("meas_we<=awaddr[11:8]==5&&wstrb==4'hf;", "meas_we<=awaddr[11:8]==5&&wstrb==4'hf;\n    sec_we<=awaddr[11:8]==7&&wstrb==4'hf;")
    t = t.replace("else if(s_axi_araddr[11:8]==6)s_axi_rdata<=buttons_rdata;", "else if(s_axi_araddr[11:8]==6)s_axi_rdata<=buttons_rdata;\n      else if(s_axi_araddr[11:8]==7)s_axi_rdata<=sec_rdata;")
    marker = " r40_cloud_view view(.aclk(aclk),.aresetn(aresetn),"
    assert t.count(marker) == 1
    t = t.replace(marker, """ // Final section snoops the same accepted geometry and commit/discard boundary.
 // It never drives ready or changes the PFO3 stream. Disabled after reset.
 section_measure section(.clk(aclk),.rst(!aresetn),
  .geo_fire(geo_out_valid&&geo_out_ready),.geo_record(geo_out_record),
  .pkt_commit(foot_last&&footer_snapshot[17]==0),
  .pkt_discard(start_pulse||abort_pulse||(foot_last&&footer_snapshot[17]!=0)),
  .reg_we(sec_we),.reg_waddr(view_waddr),.reg_wdata(view_wdata),
  .reg_raddr(s_axi_araddr[7:0]),.reg_rdata(sec_rdata));
""" + marker)
    p.write_text(t, encoding="utf-8")
    p = ROOT / originals[1]
    t = p.read_text()
    assert "section_measure.sv" not in t
    t = t.replace("r41_laser_ae.sv r41_measure.sv}", "r41_laser_ae.sv r41_measure.sv section_measure.sv}")
    p.write_text(t, encoding="utf-8")
    print(json.dumps(dict(backup=str(out), changed=originals)))


if __name__ == "__main__":
    main()
