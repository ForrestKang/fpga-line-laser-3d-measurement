"""Read-only actual-PL ABI probe shipped inside the candidate runtime.

No FPGA load, MMIO write, DMA allocation, camera, motor or old path is used.
It verifies the loaded bit/hwh against the audited manifest and reads the PFO3
frontend ABI, then writes a new receipt under the supplied new output path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "structured_light_system_algorithm_v1/src/python",
             ROOT / "structured_light_system_geometry_v1/src/python"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("ABI receipt is append-only: " + str(args.output))
    if platform.system() != "Linux" or platform.machine() not in ("armv7l", "armv7"):
        raise SystemExit("actual ARM Linux required")
    from pynq import Overlay, MMIO, PL
    from candidate.pfo3_transport_r25 import resolve_manifest
    manifest = resolve_manifest(args.manifest)
    expected = Path(manifest["bit_absolute"]).resolve()
    loaded = Path(PL.bitfile_name).resolve()
    checks = {
        "loaded_bit_path_matches": loaded == expected,
        "loaded_bit_sha_matches": bool(loaded.is_file()
                                       and sha256(loaded) == manifest["files"]["bit"]["sha256"]),
    }
    hwh = loaded.with_suffix(".hwh")
    checks["loaded_hwh_sha_matches"] = bool(hwh.is_file()
        and sha256(hwh) == manifest["files"]["hwh"]["sha256"])
    if not all(checks.values()):
        raise RuntimeError("loaded bit/hwh does not match audited V9 manifest")
    overlay = Overlay(manifest["bit_absolute"], download=False)
    for name in ("pfo3_frontend", "pfo3_dma"):
        checks["ip_" + name] = name in overlay.ip_dict
    if not checks["ip_pfo3_frontend"] or not checks["ip_pfo3_dma"]:
        raise RuntimeError("PFO3 frontend/DMA absent from loaded overlay")
    dma = overlay.ip_dict["pfo3_dma"]
    params = {str(key).lower(): value for key, value in dma["parameters"].items()}
    checks["dma_sg"] = int(params.get("c_include_sg", 0)) == 1
    checks["dma_mm2s"] = int(params.get("c_include_mm2s", 0)) == 1
    checks["dma_s2mm"] = int(params.get("c_include_s2mm", 0)) == 1
    checks["dma_sg24"] = int(params.get("c_sg_length_width", 0)) == 24
    checks["dma_stscntrl_off"] = int(params.get("c_sg_include_stscntrl_strm", -1)) == 0
    ctrl = MMIO(overlay.ip_dict["pfo3_frontend"]["phys_addr"], 4096)
    registers = {str(offset): int(ctrl.read(offset)) for offset in (0, 4, 8, 0x14, 0x1c)}
    checks["frontend_abi"] = (registers["0"], registers["4"], registers["8"]) == (
        0x57334650, 0x10000, 7)
    checks["frontend_idle"] = (registers["20"] & 1) == 0
    receipt = dict(schema="R26_BOARD_ABI_PROBE_V1", hostname=platform.node(),
                   machine_id=Path("/etc/machine-id").read_text().strip(),
                   boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                   manifest=str(args.manifest), loaded_bit=str(loaded),
                   loaded_bit_sha256=sha256(loaded) if loaded.is_file() else None,
                   expected_bit_sha256=manifest["files"]["bit"]["sha256"],
                   checks=checks, frontend_registers=registers,
                   fpga_loads=0, camera_commands=0, motor_commands=0,
                   mmio_writes=0, dma_allocations=0,
                   passed=all(checks.values()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
    print(json.dumps({"passed": receipt["passed"], "checks": checks,
                      "frontend_registers": registers}, indent=2))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
