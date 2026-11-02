"""Offline deployment ZIP staging and exclusive, verified board-side extraction.

This tool never connects to a board. The extract CLI is restricted to release01;
the caller uploads the three staged files and explicitly invokes it over SSH.
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = "FINAL_DEPLOYMENT_TRANSFER_V1"
REMOTE_ROOT = Path("/home/xilinx/scan3d_final_20261029")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or ":" in name
        or "\0" in name
    ):
        raise ValueError("invalid archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("unsafe archive path: " + name)
    return path


def regular_tree(root):
    """Reject links/reparse points, including directory junctions, before recursion."""
    found = []

    def visit(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("link/reparse point forbidden: " + str(path))
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            found.append(path)
        else:
            raise ValueError("non-regular deployment member: " + str(path))

    visit(root)
    return found


def write_exclusive(path, value):
    with path.open("x", encoding="utf-8") as out:
        json.dump(value, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def pack(source, output):
    source, output = Path(source).absolute(), Path(output).absolute()
    if not source.is_dir():
        raise ValueError("deployment directory missing")
    if output == source or source in output.parents:
        raise ValueError("transfer output must be outside deployment source")
    files = regular_tree(source)
    if not files:
        raise ValueError("empty deployment forbidden")
    entries, case_names = [], set()
    for path in files:
        name = path.relative_to(source).as_posix()
        safe_name(name)
        if name.casefold() in case_names:
            raise ValueError("duplicate case-folded source path")
        case_names.add(name.casefold())
        entries.append(
            {"path": name, "size": path.stat().st_size, "sha256": digest(path)}
        )
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "deployment.zip"
    with zipfile.ZipFile(
        archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1
    ) as zipped:
        for item in entries:
            # ZIP metadata contains only regular files, never directory/link entries.
            path = source / item["path"]
            zipped.write(path, item["path"])
            if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
                raise ValueError("source changed during packaging: " + item["path"])
    manifest = {
        "schema": SCHEMA,
        "zip_file": archive.name,
        "zip_size": archive.stat().st_size,
        "zip_sha256": digest(archive),
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
        "files": entries,
    }
    write_exclusive(output / "TRANSFER_MANIFEST.json", manifest)
    # A self-contained stdlib-only script; no runtime or PYNQ dependency.
    shutil.copyfile(Path(__file__), output / "remote_extract_final.py")
    receipt = {
        "directory": str(output),
        "file_count": len(entries),
        "total_bytes": manifest["total_bytes"],
        "zip_sha256": manifest["zip_sha256"],
        "manifest_sha256": digest(output / "TRANSFER_MANIFEST.json"),
        "extractor_sha256": digest(output / "remote_extract_final.py"),
    }
    write_exclusive(output / "LOCAL_TRANSFER_RECEIPT.json", receipt)
    return receipt


def manifest_files(manifest):
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("files"), list):
        raise ValueError("invalid transfer manifest")
    entries, names = {}, set()
    for item in manifest["files"]:
        name = item.get("path")
        safe_name(name)
        if name.casefold() in names:
            raise ValueError("duplicate manifest path")
        names.add(name.casefold())
        size, sha = item.get("size"), item.get("sha256")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(sha, str)
            or len(sha) != 64
        ):
            raise ValueError("invalid file size/SHA")
        try:
            int(sha, 16)
        except ValueError as error:
            raise ValueError("invalid file SHA") from error
        entries[name] = item
    if not entries or manifest.get("file_count") != len(entries):
        raise ValueError("manifest count mismatch")
    if manifest.get("total_bytes") != sum(item["size"] for item in entries.values()):
        raise ValueError("manifest total mismatch")
    # A file may never also be a directory prefix.
    for name in entries:
        if any(
            parent.as_posix() in entries
            for parent in PurePosixPath(name).parents
            if parent.as_posix() != "."
        ):
            raise ValueError("file/directory prefix collision")
    return entries


def extract(archive, manifest_path, target, manifest_sha256, *, allowed_root):
    """Local-testable implementation; CLI fixes allowed_root and target on the board."""
    archive, manifest_path, target = map(Path, (archive, manifest_path, target))
    allowed_root = Path(allowed_root).absolute()
    target = target.absolute()
    if target.parent != allowed_root or target.resolve() != target:
        raise ValueError("target outside exact allowed parent or linked parent")
    if target.exists() or target.is_symlink():
        raise FileExistsError("exclusive target already exists")
    if not allowed_root.is_dir():
        raise ValueError("allowed parent missing")
    if manifest_path.is_symlink() or archive.is_symlink():
        raise ValueError("linked transfer file forbidden")
    if digest(manifest_path) != manifest_sha256:
        raise ValueError("transfer manifest SHA mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest_files(manifest)
    if archive.stat().st_size != manifest.get("zip_size") or digest(
        archive
    ) != manifest.get("zip_sha256"):
        raise ValueError("ZIP size/SHA mismatch")
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        seen = set()
        for info in infos:
            name = info.filename
            safe_name(info.orig_filename)
            if info.orig_filename != name:
                raise ValueError("ZIP path was normalized by reader")
            safe_name(name)
            if name.casefold() in seen:
                raise ValueError("duplicate ZIP member")
            seen.add(name.casefold())
            mode = info.external_attr >> 16
            if info.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError("non-regular/symlink ZIP member")
            if info.flag_bits & 1 or info.compress_type not in (
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
            ):
                raise ValueError("unsupported ZIP encoding")
            if name not in entries or info.file_size != entries[name]["size"]:
                raise ValueError("ZIP member not in exact manifest or size mismatch")
        if len(infos) != len(entries):
            raise ValueError("ZIP manifest membership mismatch")
        # Validate every member before creating the exclusive destination.
        for info in infos:
            value, length = hashlib.sha256(), 0
            with zipped.open(info) as incoming:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    value.update(block)
                    length += len(block)
            expected = entries[info.filename]
            if length != expected["size"] or value.hexdigest() != expected["sha256"]:
                raise ValueError("ZIP member content mismatch: " + info.filename)
        free = shutil.disk_usage(allowed_root).free
        if free < manifest["total_bytes"] + 2 * 1024**3:
            raise ValueError("insufficient space with 2GiB system reserve")
        target.mkdir(exist_ok=False)
        for info in infos:
            path = target.joinpath(*PurePosixPath(info.filename).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as incoming, path.open("xb") as output:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if (
                path.stat().st_size != entries[info.filename]["size"]
                or digest(path) != entries[info.filename]["sha256"]
            ):
                raise ValueError("extracted content mismatch: " + info.filename)
    actual = {path.relative_to(target).as_posix() for path in regular_tree(target)}
    if actual != set(entries):
        raise ValueError("whole extracted tree differs from manifest")
    # Receipt is emitted outside release01, leaving the deployed tree exact.
    return {
        "schema": SCHEMA,
        "passed": True,
        "target": str(target),
        "file_count": len(entries),
        "total_bytes": manifest["total_bytes"],
        "manifest_sha256": manifest_sha256,
        "zip_sha256": manifest["zip_sha256"],
        "complete_tree_verified": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("pack")
    stage.add_argument("--source", required=True, type=Path)
    stage.add_argument("--output", required=True, type=Path)
    unpack = sub.add_parser("extract")
    unpack.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    if args.command == "pack":
        result = pack(args.source, args.output)
    else:
        transfer = REMOTE_ROOT / "transfer01"
        result = extract(
            transfer / "deployment.zip",
            transfer / "TRANSFER_MANIFEST.json",
            REMOTE_ROOT / "release01",
            args.manifest_sha256,
            allowed_root=REMOTE_ROOT,
        )
        write_exclusive(transfer / "EXTRACTED_FINAL.json", result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
