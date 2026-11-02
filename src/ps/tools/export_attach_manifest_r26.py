"""Export a provenance-preserving attach manifest for the already-loaded V9 bittream.

The frozen source manifest ships a relative bit path in the NEW candidate.  The
actual loaded bit path is the OLD R26 path.  This tool verifies the loaded
bit/HWH hash, current boot/machine identity and actual PFO3 MMIO ABI, then writes
a NEW manifest whose only artifact-path changes point to the actual loaded files.
It never downloads/loads an FPGA, never changes PL.bitfile_name and never edits
the frozen source manifest.

Run on the ARM board with XILINX_XRT=/usr:
    sudo -E /usr/local/share/pynq-venv/bin/python3 -B \
      runtime/tools/export_attach_manifest_r26.py \
      --source-manifest runtime/r25_v9_prep/manifest.json \
      --output runtime/r25_v9_prep/attach_manifest_r26_2026102302.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("attach manifest is append-only: " + str(args.output))
    if platform.system() != "Linux" or platform.machine() not in ("armv7l", "armv7"):
        raise SystemExit("actual ARM board required")
    from pynq import Overlay, MMIO, PL
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts
    from candidate.pfo3_transport_r25 import resolve_manifest

    source_manifest = args.source_manifest.resolve()
    source_sha = sha256(source_manifest)
    source = json.loads(source_manifest.read_text(encoding="utf-8"))
    before_bit = Path(PL.bitfile_name).resolve() if PL.bitfile_name else None
    if before_bit is None:
        raise RuntimeError("PL.bitfile_name is absent")
    loaded_bit = before_bit
    loaded_hwh = loaded_bit.with_suffix(".hwh")
    if not loaded_bit.is_file() or not loaded_hwh.is_file():
        raise RuntimeError("loaded bit/HWH files are absent")
    source_bit_meta = source["files"]["bit"]
    source_hwh_meta = source["files"]["hwh"]
    if sha256(loaded_bit) != source_bit_meta["sha256"]:
        raise RuntimeError("actual loaded bit SHA does not match frozen manifest")
    if sha256(loaded_hwh) != source_hwh_meta["sha256"]:
        raise RuntimeError("actual loaded HWH SHA does not match frozen manifest")
    # Verify full artifact/contract/HWH metadata while explicitly pointing at
    # the already-loaded old files (no download).
    artifacts = verify_stage_b_artifacts(source_manifest, bit_path=loaded_bit,
                                         hwh_path=loaded_hwh)
    overlay = Overlay(str(loaded_bit), download=False)
    after_bit = Path(PL.bitfile_name).resolve() if PL.bitfile_name else None
    if after_bit != before_bit:
        raise RuntimeError("PL.bitfile_name changed during attach manifest probe")
    for name in ("pfo3_frontend", "pfo3_dma"):
        if name not in overlay.ip_dict:
            raise RuntimeError("missing loaded PFO3 IP " + name)
    ctrl = MMIO(overlay.ip_dict["pfo3_frontend"]["phys_addr"], 4096)
    registers = {str(offset): int(ctrl.read(offset)) for offset in (0, 4, 8, 0x14, 0x1c)}
    if (registers["0"], registers["4"], registers["8"]) != (0x57334650, 0x10000, 7):
        raise RuntimeError("actual PFO3 MMIO ABI mismatch")
    if registers["20"] & 1:
        raise RuntimeError("PFO3 frontend is active; refusing attach manifest")
    manifest = dict(source)
    manifest["files"] = dict(source["files"])
    manifest["files"]["bit"] = dict(source_bit_meta, path=str(loaded_bit))
    manifest["files"]["hwh"] = dict(source_hwh_meta, path=str(loaded_hwh))
    manifest["attach_provenance"] = {
        "schema": "R26_ATTACH_MANIFEST_V1",
        "source_manifest": str(source_manifest),
        "source_manifest_sha256": source_sha,
        "machine_id": Path("/etc/machine-id").read_text(encoding="ascii").strip(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(),
        "loaded_bit": str(loaded_bit),
        "loaded_bit_sha256": sha256(loaded_bit),
        "loaded_hwh": str(loaded_hwh),
        "loaded_hwh_sha256": sha256(loaded_hwh),
        "registers": registers,
        "actual_mmio_abi": "0x57334650,0x10000,7",
        "fpga_loads": 0,
        "mmio_writes": 0,
        "pl_bitfile_name_changed": False,
        "artifact_path_changes_only": True,
        "artifacts_verified": {key: artifacts[key] for key in ("bit", "hwh", "contract", "manifest")},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Re-resolve the new manifest to ensure ActualPLTransport sees the same
    # loaded bit path, then print a non-secret receipt.
    resolved = resolve_manifest(args.output)
    if Path(resolved["bit_absolute"]).resolve() != loaded_bit:
        raise RuntimeError("attach manifest resolves to a different bit path")
    print(json.dumps({
        "schema": "R26_ATTACH_MANIFEST_RESULT_V1",
        "output": str(args.output),
        "source_manifest_sha256": source_sha,
        "loaded_bit": str(loaded_bit),
        "loaded_bit_sha256": sha256(loaded_bit),
        "boot_id": manifest["attach_provenance"]["boot_id"],
        "machine_id": manifest["attach_provenance"]["machine_id"],
        "mmio_registers": registers,
        "fpga_loads": 0,
        "mmio_writes": 0,
        "passed": True,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
