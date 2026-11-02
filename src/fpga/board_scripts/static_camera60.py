"""Authorized step 6.3 only: 60s camera/pages, bounded resource sampling."""

import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START_FREE_BYTES = 6459228160
STOP_FREE_BYTES = 2 * 1024**3 + 64 * 1024**2


def storage_stop_required(free_bytes):
    # Preserve the 2 GiB system reserve, with headroom for already active work.
    return free_bytes < STOP_FREE_BYTES


def sample():
    cpu = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    mem = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
    }
    return {
        "monotonic_s": time.monotonic(),
        "cpu_ticks": cpu,
        "mem_available_kib": mem["MemAvailable"],
        "cma_free_kib": mem.get("CmaFree"),
        "filesystem_available_bytes": (
            (disk := os.statvfs(ROOT)).f_bavail * disk.f_frsize
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--expected-boot", required=True)
    args = ap.parse_args()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if boot != args.expected_boot:
        raise RuntimeError("boot changed")
    if not args.output.resolve().is_relative_to(ROOT / "data") or args.output.exists():
        raise ValueError("new output under final/data required")
    disk = os.statvfs(ROOT)
    if disk.f_bavail * disk.f_frsize < START_FREE_BYTES:
        raise RuntimeError("static camera free-space admission rejected")
    # live owns its fresh run directory. Wrapper evidence is a separate fresh sibling.
    monitor = args.output.with_name(args.output.name + "_monitor")
    monitor.mkdir(parents=True, exist_ok=False)
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join([str(ROOT / "runtime"), env_path])
        if (env_path := os.environ.get("PYTHONPATH"))
        else str(ROOT / "runtime"),
    )
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "r33.live",
        "--manifest",
        str(ROOT / "deploy/final/manifest.json"),
        "--intrinsics",
        str(ROOT / "runtime/calibration_r41/camera_intrinsics_r41.json"),
        "--laser-plane",
        str(ROOT / "runtime/calibration_r41/laser_plane_r41.json"),
        "--output",
        str(args.output),
        "--duration-s",
        "60",
        "--save-raw-every",
        "1",
        "--section-backend",
        "pl",
        "--pages",
        "A,B,C",
        "--initial-page",
        "A",
        "--motor-power-disconnected",
        "--ui-cycle-test-s",
        "10",
    ]
    samples = []
    stop_requests = []
    started = time.monotonic()
    with (monitor / "camera.log").open("x") as log:
        process = subprocess.Popen(
            cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        while process.poll() is None:
            samples.append(sample())
            # Do not kill an owner during shutdown. Ask live to stop via its documented file.
            reason = (
                "filesystem reserve reached"
                if storage_stop_required(samples[-1]["filesystem_available_bytes"])
                else "wrapper 180s soft-stop deadline"
                if time.monotonic() - started > 180
                else None
            )
            if reason and args.output.is_dir():
                (args.output / "stop.request").touch(exist_ok=True)
                if not stop_requests or stop_requests[-1]["reason"] != reason:
                    stop_requests.append({"reason": reason, "monotonic_s": time.monotonic()})
            time.sleep(1)
    samples.append(sample())
    windows = []
    for a, b in itertools.pairwise(samples):
        diff = [y - x for x, y in zip(a["cpu_ticks"], b["cpu_ticks"])]
        total = sum(diff)
        windows.append(
            {
                "start": a["monotonic_s"],
                "end": b["monotonic_s"],
                "busy_percent": 100 * (total - diff[3] - diff[4]) / total
                if total
                else 0,
                "iowait_percent": 100 * diff[4] / total if total else 0,
            }
        )
    result = {
        "schema": "FINAL_STATIC_CAMERA_60_V1",
        "boot_id": boot,
        "boot_after": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "command": cmd,
        "returncode": process.returncode,
        "wall_s": time.monotonic() - started,
        "motor_12v_disconnected": True,
        "allow_motor": False,
        "page_switch_source": "software cycle; not physical button evidence",
        "soft_stop_requests": stop_requests,
        "filesystem_reserve_bytes": 2 * 1024**3,
        "active_work_headroom_bytes": 64 * 1024**2,
        "samples": samples,
        "cpu_windows": windows,
    }
    with (monitor / "RESOURCE_MONITOR.json").open("x") as out:
        json.dump(result, out, indent=2)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("samples", "cpu_windows")}
        )
    )
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
