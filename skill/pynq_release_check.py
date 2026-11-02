#!/usr/bin/env python3
"""Offline integrity gate for an overlay manifest and its runtime guards.

This module reads files only. It does not import PYNQ, access a board, or use
the network. See pynq_release_check.md for the evidence boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from typing import Any


SCHEMA = "PYNQ_RELEASE_CHECK_V1"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CHUNK_SIZE = 1024 * 1024
LIMITS = [
    "This checks file names, sizes, and SHA-256 bindings only.",
    "It does not prove that the overlay is loaded or that the PL executes it.",
    "It does not validate AXI/MMIO behavior, image results, geometry, or physical measurement accuracy.",
    "Run against immutable release inputs; files changed concurrently during checking are outside the guarantee.",
]


class ManifestError(ValueError):
    """Raised for malformed manifests or unsafe manifest paths."""


def _safe_relative_parts(value: Any) -> tuple[str, ...]:
    """Validate a portable manifest path and return its POSIX components."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManifestError("invalid_path")
    if "\\" in value:
        raise ManifestError("backslash_path_not_portable")
    win = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if win.drive or win.root or win.is_absolute() or posix.is_absolute():
        raise ManifestError("absolute_or_drive_path")
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ManifestError("dot_or_empty_path_component")
    return tuple(raw_parts)


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_junction(path: Path) -> bool:
    check = getattr(path, "is_junction", None)
    if check is None:
        return False
    try:
        return bool(check())
    except OSError:
        return False


def _resolve_beneath(root: Path, value: Any) -> tuple[Path | None, str | None]:
    """Resolve a safe manifest path, rejecting links which escape ``root``."""
    try:
        parts = _safe_relative_parts(value)
    except ManifestError as exc:
        return None, str(exc)

    root = root.resolve(strict=True)
    if not root.is_dir():
        return None, "root_not_directory"

    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or _is_junction(current):
            try:
                resolved_link = current.resolve(strict=False)
            except (OSError, RuntimeError):
                return None, "link_resolution_failed"
            if not _within(root, resolved_link):
                return None, "link_escapes_root"

    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, "path_resolution_failed"
    if not _within(root, resolved):
        return None, "resolved_path_escapes_root"
    return resolved, None


def _sha_and_size(path: Path) -> tuple[int, str, bool]:
    """Hash a file in bounded memory and report whether its stat stayed stable."""
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        while True:
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
        after_open = os.fstat(stream.fileno())
    after_path = path.stat()
    stable = (
        before.st_size == opened.st_size == after_open.st_size == after_path.st_size == size
        and before.st_mtime_ns == after_open.st_mtime_ns == after_path.st_mtime_ns
        and before.st_ino == after_open.st_ino == after_path.st_ino
    )
    return size, digest.hexdigest(), stable


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ManifestError("duplicate_json_key")
            parsed[key] = value
        return parsed

    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8-sig"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestError(f"manifest_read_or_json_error:{type(exc).__name__}") from exc
    if not isinstance(parsed, dict):
        raise ManifestError("manifest_root_must_be_object")
    return parsed


def check_release(manifest_path: str | os.PathLike[str], runtime_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Check manifest-bound files and runtime guard hashes without mutation."""
    manifest = Path(manifest_path)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "passed": False,
        "manifest": str(manifest),
        "firmware_root": str(manifest.parent),
        "runtime_root": str(runtime_root) if runtime_root is not None else None,
        "firmware_files": [],
        "runtime_guards": {"coverage": "declared", "checked": 0, "passed": 0, "failed": 0, "items": []},
        "errors": [],
        "warnings": [],
        "limits": list(LIMITS),
    }

    try:
        data = _read_manifest(manifest)
    except ManifestError as exc:
        result["errors"].append({"scope": "manifest", "error": str(exc)})
        result["runtime_guards"]["coverage"] = "uncovered"
        result["errors"].append({"scope": "runtime_guards", "error": "not_checked_manifest_unavailable"})
        return result

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        result["errors"].append({"scope": "manifest", "error": "files_must_be_nonempty_object"})
    else:
        seen: set[str] = set()
        firmware_root = manifest.parent.resolve(strict=False)
        for label, entry in files.items():
            item: dict[str, Any] = {"label": str(label), "status": "failed"}
            if not isinstance(label, str) or not isinstance(entry, dict):
                item["error"] = "invalid_file_entry"
                result["firmware_files"].append(item)
                result["errors"].append({"scope": "firmware", "label": str(label), "error": item["error"]})
                continue
            relpath = entry.get("path")
            item["path"] = relpath
            if not isinstance(entry.get("size"), int) or isinstance(entry.get("size"), bool) or entry["size"] < 0:
                item["error"] = "invalid_expected_size"
            elif not _valid_digest(entry.get("sha256")):
                item["error"] = "invalid_expected_sha256"
            else:
                try:
                    key = "/".join(_safe_relative_parts(relpath))
                    if key in seen:
                        item["error"] = "duplicate_firmware_path"
                    else:
                        seen.add(key)
                        target, path_error = _resolve_beneath(firmware_root, relpath)
                        if path_error:
                            item["error"] = path_error
                        elif target is None or not target.is_file():
                            item["error"] = "missing_or_not_regular_file"
                        else:
                            try:
                                actual_size, actual_sha, stable = _sha_and_size(target)
                            except OSError as exc:
                                item["error"] = f"read_error:{type(exc).__name__}"
                                result["firmware_files"].append(item)
                                result["errors"].append({"scope": "firmware", "label": str(label), "path": relpath,
                                                         "error": item["error"]})
                                continue
                            item.update({"expected_size": entry["size"], "actual_size": actual_size,
                                         "expected_sha256": entry["sha256"].lower(), "actual_sha256": actual_sha,
                                         "stable_during_read": stable})
                            if not stable:
                                item["error"] = "file_changed_during_read"
                            elif actual_size != entry["size"]:
                                item["error"] = "size_mismatch"
                            elif actual_sha != entry["sha256"].lower():
                                item["error"] = "sha256_mismatch"
                            else:
                                item["status"] = "passed"
                except ManifestError as exc:
                    item["error"] = str(exc)
            result["firmware_files"].append(item)
            if item["status"] != "passed":
                result["errors"].append({"scope": "firmware", "label": str(label), "path": relpath,
                                         "error": item.get("error", "check_failed")})

    guards = data.get("runtime_guards")
    if not isinstance(guards, dict) or not guards:
        result["runtime_guards"]["coverage"] = "uncovered"
        result["errors"].append({"scope": "runtime_guards", "error": "runtime_guards_missing_or_empty"})
    else:
        guards_root: Path | None = None
        if runtime_root is not None:
            try:
                guards_root = Path(runtime_root).resolve(strict=True)
            except (OSError, RuntimeError):
                guards_root = None
        if guards_root is None or not guards_root.is_dir():
            result["errors"].append({"scope": "runtime_guards", "error": "runtime_root_missing_or_not_directory"})
        for relpath, expected_sha in guards.items():
            item = {"path": relpath, "status": "failed"}
            if not isinstance(relpath, str) or not _valid_digest(expected_sha):
                item["error"] = "invalid_guard_entry"
            elif guards_root is None or not guards_root.is_dir():
                item["error"] = "runtime_root_unavailable"
            else:
                target, path_error = _resolve_beneath(guards_root, relpath)
                if path_error:
                    item["error"] = path_error
                elif target is None or not target.is_file():
                    item["error"] = "runtime_file_missing_or_not_regular_file"
                else:
                    try:
                        actual_size, actual_sha, stable = _sha_and_size(target)
                        item.update({"expected_sha256": expected_sha.lower(), "actual_sha256": actual_sha,
                                     "actual_size": actual_size, "stable_during_read": stable})
                        if not stable:
                            item["error"] = "file_changed_during_read"
                        elif actual_sha != expected_sha.lower():
                            item["error"] = "sha256_mismatch"
                        else:
                            item["status"] = "passed"
                    except OSError as exc:
                        item["error"] = f"read_error:{type(exc).__name__}"
            result["runtime_guards"]["items"].append(item)
            result["runtime_guards"]["checked"] += 1
            if item["status"] == "passed":
                result["runtime_guards"]["passed"] += 1
            else:
                result["runtime_guards"]["failed"] += 1
                result["errors"].append({"scope": "runtime_guard", "path": relpath,
                                         "error": item.get("error", "check_failed")})

    if result["runtime_guards"]["coverage"] != "uncovered":
        result["runtime_guards"]["coverage"] = "complete"
    result["passed"] = not result["errors"]
    if not result["passed"]:
        result["warnings"].append("One or more release bindings failed; do not treat this manifest as a passing release gate.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Overlay manifest JSON; files are relative to its directory")
    parser.add_argument("--runtime-root", required=True, help="Root directory for runtime_guards relative paths")
    parser.add_argument("--output", help="Optional new JSON report path; existing files are never overwritten")
    args = parser.parse_args(argv)

    result = check_release(args.manifest, args.runtime_root)
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            output = Path(args.output)
            with output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
        except FileExistsError:
            print(json.dumps({"schema": SCHEMA, "passed": False, "errors": [
                {"scope": "output", "error": "output_already_exists_not_overwritten", "path": args.output}
            ]}, ensure_ascii=True, indent=2))
            return 2
        except OSError as exc:
            print(json.dumps({"schema": SCHEMA, "passed": False, "errors": [
                {"scope": "output", "error": f"output_write_failed:{type(exc).__name__}", "path": args.output}
            ]}, ensure_ascii=True, indent=2))
            return 2
    # ASCII-safe JSON survives legacy Windows consoles; saved reports stay UTF-8.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
