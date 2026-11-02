r"""Prepare and audit a PYNQ-Z2 SD-image write, without writing a disk.

This script deliberately stops at a reviewable, hash-verified image and a
read-only card observation.  It never opens ``\\.\PhysicalDrive*``, invokes a
flasher, requests elevation, or writes a removable volume.  The eventual
write must be performed manually with a trusted imaging tool after the card
identity has been checked again.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable
import zipfile


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ZIP = ROOT / "build/toolchain/pynq_image/pynq_z2_v3.1.1.zip"
DEFAULT_OUTPUT = ROOT / "build/toolchain/pynq_sd_materials"
DEFAULT_ZIP_SHA256 = "d7774bb6c56b79ea67e4dbfadca443c51ae6152b8c71799e79dd81e4782d6b03"
DEFAULT_MEMBER = "pynq_z2_v3.1.1.img"
DEFAULT_MEMBER_SIZE = 8_337_309_696
IMAGE_CHUNK = 16 * 1024 * 1024
DRIVE_RE = re.compile(r"^[A-Za-z]:\\?$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PrepareError(RuntimeError):
    """Raised when image preparation cannot be completed safely."""


def sha256_file(path: Path, *, chunk_size: int = IMAGE_CHUNK) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _validate_sha(value: str, name: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise PrepareError(f"{name} must be a 64-character SHA-256")
    return value.lower()


def validate_drive(value: str) -> str:
    """Accept a Windows volume letter only; reject raw device paths."""

    if not isinstance(value, str) or not DRIVE_RE.fullmatch(value):
        raise PrepareError(
            "drive must be a volume letter such as E:; raw physical-device paths are refused"
        )
    return value[0].upper() + ":\\"


def _json_result(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


def _run_powershell_json(script: str) -> list[dict[str, Any]]:
    """Read Windows storage metadata through PowerShell cmdlets only."""

    if os.name != "nt":
        return []
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        raise PrepareError("PowerShell is required for Windows disk inventory")
    command = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$ErrorActionPreference='Stop'; " + script,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrepareError(f"read-only PowerShell inventory failed: {exc}") from exc
    try:
        value = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PrepareError(f"PowerShell returned invalid JSON: {completed.stdout!r}") from exc
    records = _json_result(value)
    if not all(isinstance(item, dict) for item in records):
        raise PrepareError("PowerShell inventory did not return object records")
    return records


def collect_storage_inventory() -> dict[str, Any]:
    """Collect disks, partitions and volumes without opening any raw device."""

    if os.name != "nt":
        return {"platform": platform.platform(), "disks": [], "partitions": [], "volumes": [], "note": "Windows storage inventory not available"}
    disks = _run_powershell_json(
        "Get-Disk | Select-Object Number,FriendlyName,BusType,PartitionStyle,Size,"
        "OperationalStatus,IsBoot,IsSystem,IsReadOnly,IsOffline | ConvertTo-Json -Compress"
    )
    partitions = _run_powershell_json(
        "Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,Size,"
        "Type,IsActive,Offset | ConvertTo-Json -Compress"
    )
    volumes = _run_powershell_json(
        "Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,Size,"
        "SizeRemaining,Path,DriveType | ConvertTo-Json -Compress"
    )
    return {
        "platform": platform.platform(),
        "disks": disks,
        "partitions": partitions,
        "volumes": volumes,
    }


def _as_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _drive_entry(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:1].upper() if text else None


def _removable_volume_letters(inventory: dict[str, Any]) -> set[str]:
    letters = set()
    for item in inventory.get("volumes", []):
        drive = _drive_entry(item.get("DriveLetter"))
        kind = item.get("DriveType")
        is_removable = str(kind).strip().lower() == "removable"
        if not is_removable:
            try:
                is_removable = int(kind) == 2
            except (TypeError, ValueError):
                pass
        if drive and is_removable:
            letters.add(drive)
    return letters


def observe_drive(drive: str, inventory: dict[str, Any]) -> dict[str, Any]:
    """Join a volume with its partition/disk and inspect only its root listing."""

    root = Path(validate_drive(drive))
    letter = root.drive[:1].upper()
    volumes = [item for item in inventory.get("volumes", []) if _drive_entry(item.get("DriveLetter")) == letter]
    partitions = [item for item in inventory.get("partitions", []) if _drive_entry(item.get("DriveLetter")) == letter]
    if not volumes or not partitions:
        raise PrepareError(f"drive {letter}: was not present in the read-only storage inventory")
    volume = volumes[0]
    partition = partitions[0]
    disk_number = _as_int(partition.get("DiskNumber"))
    disks = [item for item in inventory.get("disks", []) if _as_int(item.get("Number")) == disk_number]
    if not disks:
        raise PrepareError(f"drive {letter}: owning disk was not present in the inventory")
    disk = disks[0]
    if bool(disk.get("IsBoot")) or bool(disk.get("IsSystem")):
        raise PrepareError(f"drive {letter}: boot/system disk is refused")
    entries = []
    try:
        for item in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            try:
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size": stat.st_size if item.is_file() else None,
                })
            except OSError as exc:
                entries.append({"name": item.name, "stat_error": str(exc)})
    except OSError as exc:
        raise PrepareError(f"cannot read {root} root listing: {exc}") from exc
    return {
        "drive": f"{letter}:",
        "root": str(root),
        "disk": disk,
        "partition": partition,
        "volume": volume,
        "root_entries": entries,
        "read_only_observation": True,
        "raw_device_opened": False,
    }


def _safe_member(name: str, expected: str) -> None:
    if name != expected:
        raise PrepareError(f"ZIP member {name!r} is not the expected {expected!r}")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.name != name:
        raise PrepareError(f"unsafe ZIP member path: {name!r}")


def _verify_zip(path: Path, expected_sha256: str, expected_member: str, expected_size: int) -> dict[str, Any]:
    if not path.is_file():
        raise PrepareError(f"official ZIP not found: {path}")
    expected_sha256 = _validate_sha(expected_sha256, "expected ZIP SHA-256")
    actual_sha, zip_size = sha256_file(path)
    if actual_sha != expected_sha256:
        raise PrepareError(f"ZIP SHA-256 mismatch: {actual_sha} != {expected_sha256}")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PrepareError(f"cannot open official ZIP: {path}: {exc}") from exc
    with archive:
        bad = archive.testzip()
        if bad:
            raise PrepareError(f"ZIP CRC check failed for {bad!r}")
        matches = [item for item in archive.infolist() if item.filename == expected_member]
        if len(matches) != 1:
            raise PrepareError(f"ZIP must contain exactly one {expected_member!r}; found {len(matches)}")
        member = matches[0]
        _safe_member(member.filename, expected_member)
        if member.file_size != expected_size:
            raise PrepareError(f"image member size {member.file_size} != expected {expected_size}")
    return {
        "zip_path": str(path),
        "zip_sha256": actual_sha,
        "zip_size": zip_size,
        "member": expected_member,
        "member_size": expected_size,
        "crc_verified": True,
    }


def extract_image(
    zip_path: Path,
    output_dir: Path,
    *,
    member_name: str = DEFAULT_MEMBER,
    member_size: int = DEFAULT_MEMBER_SIZE,
    zip_sha256: str | None = None,
    expected_image_sha256: str | None = None,
) -> dict[str, Any]:
    """Extract only the expected IMG to a workspace path, atomically."""

    output_dir.mkdir(parents=True, exist_ok=True)
    verification = _verify_zip(
        zip_path,
        zip_sha256 or sha256_file(zip_path)[0],
        member_name,
        member_size,
    )
    target = output_dir / member_name
    if target.exists():
        actual_sha, actual_size = sha256_file(target)
        if actual_size != member_size:
            raise PrepareError(f"existing IMG has size {actual_size}; refusing overwrite: {target}")
        if expected_image_sha256 is not None and actual_sha != _validate_sha(expected_image_sha256, "expected IMG SHA-256"):
            raise PrepareError(f"existing IMG SHA-256 mismatch: {actual_sha}")
        return {**verification, "image_path": str(target), "image_sha256": actual_sha,
                "image_size": actual_size, "extracted": False, "reused_verified": True}

    temporary = output_dir / f".{member_name}.partial"
    if temporary.exists():
        raise PrepareError(f"partial IMG already exists; inspect/remove it manually: {temporary}")
    digest = hashlib.sha256()
    size = 0
    try:
        with zipfile.ZipFile(zip_path) as archive, archive.open(member_name, "r") as source:
            with temporary.open("xb") as sink:
                while block := source.read(IMAGE_CHUNK):
                    digest.update(block)
                    size += len(block)
                    sink.write(block)
                sink.flush()
                os.fsync(sink.fileno())
        actual_sha = digest.hexdigest()
        if size != member_size:
            raise PrepareError(f"extracted IMG size {size} != expected {member_size}")
        if expected_image_sha256 is not None and actual_sha != _validate_sha(expected_image_sha256, "expected IMG SHA-256"):
            raise PrepareError(f"extracted IMG SHA-256 mismatch: {actual_sha}")
        os.replace(temporary, target)
    except Exception:
        # Keep a failed partial for forensic inspection, never replace an
        # existing image with an incomplete output.
        raise
    return {**verification, "image_path": str(target), "image_sha256": actual_sha,
            "image_size": size, "extracted": True, "reused_verified": False}


def _tool_status(paths: Iterable[Path]) -> list[dict[str, Any]]:
    result = []
    for path in paths:
        if path.is_file():
            digest, size = sha256_file(path)
            result.append({"path": str(path), "present": True, "size": size,
                           "sha256": digest, "signature_status": "not_checked"})
    return result


def tool_recommendations() -> dict[str, Any]:
    program_files = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                     Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    imager_paths = [item / "Raspberry Pi Imager" / "rpi-imager.exe" for item in program_files]
    if local:
        imager_paths.append(local / "Programs/Raspberry Pi Imager/rpi-imager.exe")
    win32_paths = [item / "Win32DiskImager" / "Win32DiskImager.exe" for item in program_files]
    return {
        "preferred": {
            "name": "Raspberry Pi Imager",
            "source": "https://www.raspberrypi.com/software/",
            "usage": "Choose Use custom image, select the verified IMG, select the rechecked SD volume, then write and verify.",
            "signature_status": "not_downloaded_or_checked_by_this_script",
            "official_release_page": "https://github.com/raspberrypi/rpi-imager/releases",
            "observed_windows_asset": "imager-v2.0.11.1.exe",
            "observed_asset_sha256": "94ffded522f3e2a38bdb9505440229e1411b80992a616ba16b2d7e73bd794130",
        },
        "alternative": {
            "name": "Win32 Disk Imager",
            "source": "https://sourceforge.net/projects/win32diskimager/",
            "usage": "Select the verified IMG and the rechecked removable drive letter; use its read-back/verify workflow if available.",
            "signature_status": "not_downloaded_or_checked_by_this_script",
        },
        "candidate_paths": {
            "raspberry_pi_imager": [str(path) for path in imager_paths],
            "win32_disk_imager": [str(path) for path in win32_paths],
        },
        "installed_paths": {
            "raspberry_pi_imager": _tool_status(imager_paths),
            "win32_disk_imager": _tool_status(win32_paths),
        },
        "install_or_write_performed": False,
    }


def prepare(
    *,
    zip_path: Path,
    output_dir: Path,
    drive: str | None,
    expected_zip_sha256: str = DEFAULT_ZIP_SHA256,
    expected_member: str = DEFAULT_MEMBER,
    expected_member_size: int = DEFAULT_MEMBER_SIZE,
    expected_image_sha256: str | None = None,
    inventory_provider: Callable[[], dict[str, Any]] = collect_storage_inventory,
) -> dict[str, Any]:
    """Create hash-verified image material and a read-only SD observation."""

    zip_path = zip_path.resolve()
    output_dir = output_dir.resolve()
    if expected_member_size <= 0:
        raise PrepareError("expected image member size must be positive")
    # Do not silently use the fixed vendor digest for arbitrary test/custom
    # paths.  Callers can explicitly provide the expected digest.
    inventory = inventory_provider()
    observation = None
    if drive is not None:
        observation = observe_drive(drive, inventory)
    if os.name == "nt" and output_dir.drive:
        output_drive = output_dir.drive[:1].upper()
        removable = _removable_volume_letters(inventory)
        if output_drive in removable:
            raise PrepareError(
                f"output directory is on removable volume {output_drive}:; refusing to write SD material there"
            )
        if drive is not None and output_drive == validate_drive(drive)[:1]:
            raise PrepareError(
                f"output directory shares target volume {output_drive}:; refusing to write SD material there"
            )
    zip_verification = _verify_zip(zip_path, expected_zip_sha256, expected_member, expected_member_size)
    image = extract_image(
        zip_path, output_dir, member_name=expected_member,
        member_size=expected_member_size, zip_sha256=zip_verification['zip_sha256'],
        expected_image_sha256=expected_image_sha256,
    )
    if drive is not None:
        disk_size = _as_int(observation["disk"].get("Size"))
        if disk_size is not None and disk_size < image["image_size"]:
            raise PrepareError(f"SD disk capacity {disk_size} is smaller than IMG {image['image_size']}")
    result = {
        "schema_version": 1,
        "prepared_utc": datetime.now(timezone.utc).isoformat(),
        "image": image,
        "zip_verification": zip_verification,
        "storage_inventory": inventory,
        "sd_observation": observation,
        "tools": tool_recommendations(),
        "safety": {
            "sd_written": False,
            "raw_device_opened": False,
            "uac_requested": False,
            "flasher_invoked": False,
            "write_operation": "not_performed",
        },
        "next_manual_action": "Recheck the removable disk identity and use a trusted image writer manually; do not select Disk 0/NVMe.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "pynq_sd_prepare.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (output_dir / "SD_WRITE_INSTRUCTIONS.md").write_text(_instructions(result), encoding="utf-8")
    return result


def _instructions(result: dict[str, Any]) -> str:
    image = result["image"]
    observation = result.get("sd_observation")
    return f"""# PYNQ-Z2 SD 写入材料（人工最后一步）

本目录由 `prepare_pynq_sd.py` 生成。脚本只校验 ZIP、解压 IMG、读取磁盘信息，
没有打开物理磁盘、没有申请管理员权限，也没有写入 SD。

## 已验证镜像

- IMG：`{image['image_path']}`
- IMG bytes：`{image['image_size']}`
- IMG SHA-256：`{image['image_sha256']}`
- ZIP SHA-256：`{image['zip_sha256']}`
- ZIP member：`{image['member']}`

## 当前只读卡观察

```json
{json.dumps(observation, ensure_ascii=False, indent=2, default=str)}
```

## 推荐工具

1. **Raspberry Pi Imager（优先）**：<https://www.raspberrypi.com/software/>
   选择 `Use custom image`，选上面的 IMG，再重新核对可移动盘容量和盘号后写入，
   完成后执行工具提供的验证。官方安装包和发布校验值应从官方页面/官方 release 获取；
   本脚本没有下载、安装或校验该工具。当前观察到的官方 release asset 是
   `imager-v2.0.11.1.exe`，SHA-256 为
   `94ffded522f3e2a38bdb9505440229e1411b80992a616ba16b2d7e73bd794130`；
   下载后仍需人工核对发布页和 Authenticode 签名。候选安装路径：
   `{json.dumps(result['tools']['candidate_paths']['raspberry_pi_imager'], ensure_ascii=False)}`。
2. **Win32 Disk Imager（备选）**：<https://sourceforge.net/projects/win32diskimager/>
   选择上面的 IMG 和重新确认的可移动盘符，再执行写入/回读验证。下载文件的签名或
   SHA-256 需要人工从发布页核对；本脚本没有下载、安装或启动它。候选安装路径：
   `{json.dumps(result['tools']['candidate_paths']['win32_disk_imager'], ensure_ascii=False)}`。

写入前再次确认：目标必须是当前 USB 可移动 SD，不能是系统 NVMe Disk 0。写入后
Windows 可能提示格式化未知分区；不要格式化，直接安全弹出并切换 PYNQ-Z2 的 SD 启动。

`sd_written=false`、`flasher_invoked=false` 是本材料的固定边界。真实写卡、启动、SSH、
Jupyter 和 PYNQ 环境验收另行记录。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--drive", help="read-only volume letter such as E:; never a PhysicalDrive path")
    parser.add_argument("--expected-zip-sha256", default=DEFAULT_ZIP_SHA256)
    parser.add_argument("--expected-member", default=DEFAULT_MEMBER)
    parser.add_argument("--expected-member-size", type=int, default=DEFAULT_MEMBER_SIZE)
    parser.add_argument("--expected-image-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prepare(
            zip_path=args.zip, output_dir=args.output, drive=args.drive,
            expected_zip_sha256=args.expected_zip_sha256,
            expected_member=args.expected_member,
            expected_member_size=args.expected_member_size,
            expected_image_sha256=args.expected_image_sha256,
        )
    except (OSError, PrepareError, ValueError, zipfile.BadZipFile) as exc:
        print(f"SD preparation failed without touching a disk: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MEMBER", "DEFAULT_MEMBER_SIZE", "DEFAULT_OUTPUT", "DEFAULT_ZIP",
    "DEFAULT_ZIP_SHA256", "PrepareError", "collect_storage_inventory",
    "extract_image", "observe_drive", "prepare", "sha256_file", "tool_recommendations",
    "validate_drive",
]
