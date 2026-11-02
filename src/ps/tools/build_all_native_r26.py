"""Build every native library used by the R26 formal entry, without downloading.

This is an explicit CPU-only build step for a fresh deployment.  It refuses to
overwrite any existing library or manifest.  The native geometry/linker/quality
libraries are built first, then the R26 assembler, then the optional strict body
validator with its source/runtime-bound QA manifest.

No FPGA load, board access, camera, motor or network operation is performed.
The compile flags preserve IEEE double semantics: no fast-math, contraction
disabled for the geometry libraries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
NATIVE = ROOT / "candidate" / "native"
ASSEMBLE_SOURCE = NATIVE / "assemble_r26.c"
ASSEMBLE_OUTPUT = NATIVE / ("assemble_r26.dll" if os.name == "nt" else "libassemble_r26.so")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command, *, cwd=ROOT, env=None):
    result = subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("native build failed: %s\n%s\n%s" % (
            " ".join(map(str, command)), result.stdout, result.stderr))
    return dict(command=[str(item) for item in command],
                stdout=result.stdout, stderr=result.stderr)


def _assert_absent(path: Path):
    if path.exists() or path.is_symlink():
        raise FileExistsError("preserve existing native artifact: " + str(path))


def build_assembler() -> dict:
    _assert_absent(ASSEMBLE_OUTPUT)
    command = ["gcc", "-O3", "-fno-strict-aliasing", "-std=c99",
               "-Wall", "-Wextra", "-Werror", "-shared"]
    if os.name != "nt":
        command.append("-fPIC")
    command.extend([str(ASSEMBLE_SOURCE), "-o", str(ASSEMBLE_OUTPUT), "-lz"])
    result = _run(command)
    return dict(source=str(ASSEMBLE_SOURCE), source_sha256=sha256(ASSEMBLE_SOURCE),
                output=str(ASSEMBLE_OUTPUT), output_sha256=sha256(ASSEMBLE_OUTPUT),
                **result)


def build_frozen_native() -> dict:
    # Reuse the established explicit builder; it refuses every existing output.
    from tools.build_native_r22 import plan
    rows = plan()
    for row in rows:
        _assert_absent(Path(row["output"]))
    for row in rows:
        result = _run(row["command"])
        row.update(stderr=result["stderr"], output_sha256=sha256(Path(row["output"])))
    return dict(libraries=rows)


def build_optional_native() -> list[dict]:
    """Compile new, explicitly named R26 helpers that are present in source.

    The four frozen libraries and the assembler/validator are handled by their
    existing builders above.  Every other source under candidate/native is an
    optional helper; it is built with the same no-fast-math flags, and the
    receipt records source/output SHA.  A missing helper is not an error.
    """
    known = {"multibranch_r21.c", "quality_r21.c", "geometry_r21.c",
             "link_candidates_r22.c", "assemble_r26.c", "pfo3_validate_r25.c"}
    rows = []
    for source in sorted(NATIVE.glob("*.c")):
        if source.name in known:
            continue
        # Match the frozen builder convention: lib<stem>.so on POSIX and
        # <stem>.dll on Windows.  Loaders in the runtime use these exact names.
        output = source.with_name(
            (source.stem + ".dll") if os.name == "nt" else ("lib" + source.stem + ".so"))
        _assert_absent(output)
        command = ["gcc", "-O3", "-std=c99", "-fno-fast-math",
                   "-ffp-contract=off", "-fexcess-precision=standard",
                   "-frounding-math", "-Wall", "-Wextra", "-Werror", "-shared"]
        if os.name != "nt":
            command.append("-fPIC")
        command.extend([str(source), "-o", str(output), "-lm"])
        result = _run(command)
        rows.append(dict(source=str(source), source_sha256=sha256(source),
                         output=str(output), output_sha256=sha256(output),
                         optional=True, **result))
    return rows


def build_validator() -> dict:
    output = NATIVE / ("pfo3_validate_r25.dll" if os.name == "nt" else "libpfo3_validate_r25.so")
    manifest = NATIVE / "pfo3_validate_manifest_r25.json"
    qa = NATIVE / "pfo3_validate_qa_r25.json"
    for path in (output, manifest, qa):
        _assert_absent(path)
    command = [sys.executable, "-B", str(ROOT / "tools" / "build_pfo3_validator_r25.py"), "--build"]
    result = _run(command)
    for path in (output, manifest, qa):
        if not path.is_file():
            raise RuntimeError("validator build did not produce " + str(path))
    return dict(output=str(output), output_sha256=sha256(output),
                manifest=str(manifest), manifest_sha256=sha256(manifest),
                qa=str(qa), qa_sha256=sha256(qa), **result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=NATIVE / "R26_NATIVE_BUILD.json",
                        help="new receipt path; existing receipts are never overwritten")
    args = parser.parse_args(argv)
    if platform.system() not in ("Linux", "Windows"):
        raise RuntimeError("explicit Windows/Linux build host required")
    if args.output.exists():
        raise FileExistsError("preserve existing build receipt: " + str(args.output))
    receipt = dict(schema="R26_NATIVE_BUILD_V1", platform=platform.platform(),
                   machine=platform.machine(), fast_math=False,
                   board_access=False, fpga_loads=0,
                   frozen=build_frozen_native(),
                   optional=build_optional_native(),
                   assembler=build_assembler(),
                   validator=build_validator())
    data = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"receipt": str(args.output), "sha256": sha256(args.output),
                      "assembler_sha256": receipt["assembler"]["output_sha256"],
                      "validator_manifest_sha256": receipt["validator"]["manifest_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
