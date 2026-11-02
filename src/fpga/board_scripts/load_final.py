"""One boot-bound final-board download; no camera, motion or boot edits."""

import argparse
import hashlib
import json
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = Path("/home/xilinx/scan3d_final_20261029")
MACHINE = "c82cf5bcd3a14b95935d65bfb529d72d"
ABI = (0x57334650, 0x35000, 31)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity():
    return (
        Path("/etc/machine-id").read_text().strip(),
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )


def save(path, value):
    with Path(path).open("x", encoding="utf8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def validate(proof, manifest, manifest_sha, machine, boot, name):
    if not (
        machine == MACHINE == proof.get("machine_id")
        and boot == proof.get("boot_id")
        and proof.get("manifest_sha256") == manifest_sha
        and proof.get("load_name") == name
        and proof.get("physical_motor_12v_disconnected") is True
        and proof.get("load_authorized") is True
        and proof.get("source") == "explicit_user_authorization"
        and isinstance(proof.get("user_statement"), str)
        and proof["user_statement"].strip()
    ):
        raise ValueError("boot/machine/manifest/physical-12V/user authority mismatch")
    sec = manifest.get("section_measure", {})
    if (
        manifest.get("motion_enabled") is not False
        or manifest.get("pfo3", {}).get("n_lanes") != 2
        or sec.get("present") is not True
        or sec.get("register_window") != [0x700, 0x7FF]
        or sec.get("magic") != 0x5345434D
        or sec.get("version") != 1
    ):
        raise ValueError("two-lane final static-camera manifest required")


def observe(mmio, section=False):
    offsets = [0, 4, 8, 0x14, 0x18, 0x1C]
    if section:
        offsets += [0x700, 0x704]
    result = {hex(o): int(mmio.read(o)) for o in offsets}
    if tuple(result[hex(o)] for o in (0, 4, 8)) != ABI:
        raise ValueError("actual frontend ABI mismatch")
    if result["0x14"] & 1:
        raise ValueError("frontend busy")
    # 0x704 is control, not a version ID: reset = use_preview(1), enable(0).
    if section and (result["0x700"] != 0x5345434D or result["0x704"] != 2):
        raise ValueError("actual section ABI mismatch")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--proof", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("armv7l", "armv7"):
        raise RuntimeError("actual ARM board required")
    for path, parent in (
        (args.manifest, REMOTE),
        (args.proof, REMOTE),
        (args.output, REMOTE / "data"),
    ):
        if not path.resolve().is_relative_to(parent):
            raise ValueError("outside new candidate")
    if args.output.exists():
        raise FileExistsError("load ID already consumed")
    machine, boot = identity()
    manifest = json.loads(args.manifest.read_text())
    proof = json.loads(args.proof.read_text())
    validate(proof, manifest, sha(args.manifest), machine, boot, args.output.name)
    sys.path[:0] = [
        str(ROOT / "runtime"),
        str(ROOT / "runtime/structured_light_system_algorithm_v1/src/python"),
        str(ROOT / "runtime/structured_light_system_geometry_v1/src/python"),
    ]
    from candidate.pfo3_hwh_r25 import audit_hwh
    from structured_light.motion.lifecycle import HardwareProcessLock
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts

    artifacts = verify_stage_b_artifacts(args.manifest)
    audit_hwh(artifacts["hwh"])
    for name, expected in manifest["runtime_guards"].items():
        if sha(ROOT / "runtime" / name) != expected:
            raise ValueError("runtime guard differs: " + name)
    args.output.mkdir(parents=True, exist_ok=False)
    record = {
        "schema": "FINAL_BOOT_BOUND_LOAD_V1",
        "machine_id": machine,
        "boot_id": boot,
        "proof": proof,
        "manifest_sha256": sha(args.manifest),
        "FPGA_load_attempts": 0,
        "FPGA_loads": 0,
        "camera_commands": 0,
        "motor_commands": 0,
        "boot_files_modified": False,
        "automatic_retry": False,
        "passed": False,
    }
    lock = HardwareProcessLock(label="final_explicit_static_load").acquire()
    try:
        from pynq import MMIO, PL, Overlay

        if identity() != (machine, boot):
            raise RuntimeError("identity changed before download")
        before = MMIO(manifest["pfo3"]["frontend_base"], 4096)
        record["before_registers"] = observe(before)
        record["previous_bit"] = PL.bitfile_name
        save(args.output / "ADMISSION.json", record)
        if identity() != (machine, boot):
            raise RuntimeError("identity changed at download boundary")
        record["FPGA_load_attempts"] = 1
        save(args.output / "LOAD_ATTEMPT.json", record)
        overlay = Overlay(artifacts["bit"], download=True)
        record["FPGA_loads"] = 1
        if identity() != (machine, boot):
            raise RuntimeError("identity changed after download")
        loaded = Path(PL.bitfile_name).resolve()
        if (
            loaded != Path(artifacts["bit"]).resolve()
            or sha(loaded) != manifest["files"]["bit"]["sha256"]
        ):
            raise ValueError("loaded bit identity mismatch")
        if not all(n in overlay.ip_dict for n in ("pfo3_frontend", "pfo3_dma")):
            raise ValueError("required IP absent")
        after = MMIO(overlay.ip_dict["pfo3_frontend"]["phys_addr"], 4096)
        record.update(
            after_registers=observe(after, section=True),
            passed=True,
            loaded_bit=str(loaded),
            bit_sha256=sha(loaded),
            hwh_sha256=manifest["files"]["hwh"]["sha256"],
            n_lanes=2,
        )
        save(args.output / "LOADED_FINAL_STATIC.json", record)
        print(json.dumps(record))
    except BaseException as exc:
        record.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        save(args.output / "ERROR.json", record)
        raise
    finally:
        lock.release()


if __name__ == "__main__":
    main()
