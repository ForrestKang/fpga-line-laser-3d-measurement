"""Read-only contract and baseline checks; no board/EDA dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re

import yaml

from .contract_tools import expected_artifacts, read_contract


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    checks: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def project_root() -> Path:
    for candidate in (Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[3]):
        if (candidate / "src/contracts/interface.json").is_file():
            return candidate
    raise FileNotFoundError("project contract not found; run from the project root")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_fingerprints(root: Path) -> dict[str, str]:
    """Content fingerprint for reproducibility inputs, excluding mutable run reports."""
    patterns = ["src/**/*.py", "src/**/*.json", "src/configs/*.yaml", "sim/tests/*.py", "build/scripts/*.py", "board/configs/*.yaml", "skill/**/*.py", "skill/**/*.json", "skill/**/README.md", "data/baseline/*", "data/fixtures/*", "data/reference/*", "README.md", "license.md", "pyproject.toml", "requirements-test-lock.txt"]
    files = {path for pattern in patterns for path in root.glob(pattern) if path.is_file()}
    return {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError(f"manifest path escapes root: {relative}")
    return path


def verify_baselines(baseline_root: str | Path, *, project: str | Path | None = None) -> CheckResult:
    """Compare protected baseline files under a workspace against the frozen manifest.

    ``baseline_root`` is the parent directory containing the three old projects;
    it is supplied explicitly and is never used for Python imports.
    """
    root = Path(project) if project is not None else project_root()
    errors, checked = [], 0
    try:
        manifest = _json(root / "data/baseline/manifest.json")
        records = manifest["records"]
        if not records:
            raise ValueError("empty baseline manifest")
        seen = set()
        for record in records:
            relative = record["path"]
            if relative in seen:
                raise ValueError(f"duplicate baseline path: {relative}")
            seen.add(relative)
            path = _safe_path(Path(baseline_root), relative)
            if not path.is_file():
                errors.append(f"missing: {relative}")
                continue
            raw = path.read_bytes()
            if len(raw) != record["size"] or sha256(raw).hexdigest() != record["sha256"]:
                errors.append(f"changed: {relative}")
            checked += 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    return CheckResult(not errors, ("external_baseline_fingerprints",), tuple(errors), {"checked_files": checked, "root": str(Path(baseline_root).resolve()), "scope": "selected manifest files only"})


def check_snapshot_integrity(root: Path) -> CheckResult:
    errors, checked = [], 0
    for manifest_path, array_key, prefix in [("data/fixtures/manifest.json", "fixtures", "data/fixtures"), ("data/reference/snapshot_manifest.json", "records", "")]:
        try:
            manifest = _json(root / manifest_path)
            for item in manifest[array_key]:
                path = _safe_path(root, (Path(prefix) / item["path"]).as_posix())
                raw = path.read_bytes()
                if sha256(raw).hexdigest() != item["sha256"] or len(raw) != item["size"]:
                    errors.append(f"snapshot changed: {item['path']}")
                checked += 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{manifest_path}: {exc}")
    return CheckResult(not errors, ("portable_snapshot_integrity",), tuple(errors), {"checked_files": checked})


def check_register_baseline(spec: dict, source: str) -> list[str]:
    """Independently extract offsets, access groups and resets from frozen old RTL."""
    errors = []
    addresses = {symbol: int(value, 16) for symbol, value in re.findall(r"localparam\s+\[7:0\]\s+(A_\w+)\s*=\s*8'h([0-9a-fA-F]+)", source)}
    access = {}
    for function, mode in [("write_config_addr", "rw"), ("readonly_addr", "ro")]:
        match = re.search(r"function automatic " + function + r";(.+?)endfunction", source, re.S)
        if not match:
            return [f"baseline function absent: {function}"]
        case = match.group(1).split(": " + function + " =")[0]
        for symbol in re.findall(r"A_\w+", case):
            access[symbol] = mode
    access.update(A_CONTROL="command", A_IRQ_STATUS="w1c")
    resets = {int(index): int(value.replace("_", "")) for index, value in re.findall(r"cfg_words\[(\d+)\]\s*<=\s*32'd([\d_]+)", source)}
    roi = re.search(r"cfg_words\[4\]\s*<=\s*\{16'd(\d+),\s*16'd(\d+)\}", source)
    if roi:
        resets[4] = (int(roi[1]) << 16) | int(roi[2])
    version = re.search(r"A_VERSION:\s*read_mux_data\s*=\s*32'h([\da-fA-F_]+)", source)
    if version:
        resets[30] = int(version[1].replace("_", ""), 16)
    if len(addresses) != len(spec["registers"]):
        errors.append("register count differs from old RTL")
    for reg in spec["registers"]:
        symbol = reg["baseline_symbol"]
        if addresses.get(symbol) != reg["offset"]:
            errors.append(f"offset differs: {reg['name']}")
        if access.get(symbol) != reg["access"]:
            errors.append(f"access differs: {reg['name']}")
        if "reset" in reg and resets.get(reg["offset"] // 4) != reg["reset"]:
            errors.append(f"reset differs: {reg['name']}")
    return errors


def check_layout(root: Path) -> CheckResult:
    required = ["src/contracts", "src/configs", "src/python/structured_light", "src/rtl/generated", "sim/tests", "sim/results", "build/scripts", "build/reports", "board/configs", "board/scripts", "board/notebooks", "board/results", "data/baseline", "data/fixtures", "data/reference", "skill/interface_contract_checks/examples", "report/ai_collaboration", "report/figures"]
    errors = [f"missing directory: {p}" for p in required if not (root / p).is_dir()]
    excluded = {".venv", ".git", "__pycache__", ".pytest_cache", ".reproduction", ".Xil", ".vivado-ai"}
    # Vendor case is part of the upstream package, not a custom source name.
    # Only manifest-listed, hash-checked files and their parent directories
    # receive this exception. Git can convert LF to CRLF; no other textual
    # normalization is allowed.
    vendor_allowed = set()
    vendor = root / "build/vendor/pynq_v3_1"
    manifest_path = vendor / "source_manifest.json"
    if manifest_path.is_file():
        for item in _json(manifest_path)["files"]:
            file = _safe_path(vendor, item["path"])
            if not file.is_file() or sha256(file.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != item["sha256"]:
                errors.append(f"vendor source fingerprint mismatch: {item['path']}")
                continue
            vendor_allowed.add(file.resolve())
            vendor_allowed.update(p.resolve() for p in file.parents if p.is_relative_to(vendor))
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in excluded or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.parts[:2] == ("build", "runtime") or relative.as_posix() == "clockInfo.txt":
            continue  # Vivado-generated local state; never a source deliverable.
        if path.name in {"README.md", ".gitignore", ".gitattributes"} or path.resolve() in vendor_allowed:
            continue
        if not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*", path.name):
            errors.append(f"nonportable name: {relative.as_posix()}")
    return CheckResult(not errors, ("layout_and_names",), tuple(errors), {"required_directories": len(required)})


def check_contracts(*, root: str | Path | None = None) -> CheckResult:
    root = Path(root) if root is not None else project_root()
    errors, checks = [], []
    try:
        spec = read_contract(root / "src/contracts/interface.json")
        checks.append("json_schema")
        for p in spec["profiles"]:
            prefix = p["name"] + "_"
            for constant, value in [("MAGIC", p["magic"]), ("HEADER_WORDS", len(p["header"])), ("VALID_BIT", p["valid_bit"])]:
                if spec["constants"].get(prefix+constant) != value:
                    errors.append(f"profile/constant mismatch: {prefix+constant}")
        if spec["profiles"][0]["header"] != ["magic", "profile_id", "angle_raw", "timestamp", "valid_count", "error_flags", "point_count"] or spec["profiles"][1]["header"] != ["magic", "profile_id", "angle_raw", "timestamp_low", "timestamp_high", "valid_count", "error_flags", "point_count"]:
            errors.append("legacy header layout changed; requires an explicitly versioned new codec")
        checks.append("legacy_layout_and_constants")
        for relative, content in expected_artifacts(root).items():
            if not (root / relative).is_file() or (root / relative).read_text(encoding="utf-8") != content:
                errors.append(f"generated artifact stale: {relative}")
        checks.append("python_sv_document_consistency")
        errors += check_register_baseline(spec, (root / "data/reference/sl_control_axi.sv").read_text(encoding="utf-8"))
        checks.append("register_offsets_access_and_resets_vs_old_rtl")
        from .arithmetic import row_accumulator_bounds
        from .config import load_config
        for bounds in spec["centroid"]["worst_case"]:
            actual = row_accumulator_bounds(bounds["width"])
            for attribute, key in [("max_s0", "max_s0"), ("max_s1", "max_s1"), ("s0_bits", "s0_bits"), ("s1_bits", "s1_bits"), ("q12_numerator_bits", "numerator_bits")]:
                if getattr(actual, attribute) != bounds[key]:
                    errors.append(f"arithmetic mismatch {bounds['width']}: {key}")
        checks.append("640_752_unsigned_ranges")
        for path in (root / "src/configs").glob("*.yaml"):
            cfg = load_config(path)
            if cfg.system_clock_hz != spec["constants"]["SYSTEM_CLOCK_HZ"] or cfg.fractional_bits != spec["constants"]["XC_FRACTION_BITS"]:
                errors.append(f"config contract mismatch: {path.name}")
            if path.name == "default.yaml":
                for attribute, register in [("threshold_dn", "THRESHOLD"), ("min_width_px", "MIN_WIDTH"), ("max_width_px", "MAX_WIDTH"), ("min_sum_dn", "MIN_SUM"), ("min_peak_dn", "MIN_PEAK")]:
                    reset = next(reg["reset"] for reg in spec["registers"] if reg["name"] == register)
                    if getattr(cfg, attribute) != reset:
                        errors.append(f"default changed from old baseline: {register}")
        checks.append("offline_configs")
        for path in (root / "board/configs").glob("*.yaml"):
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            if path.name in {"p2_preboard.yaml", "p2_manual_wiring.yaml", "p2_closeout_reference.yaml"}:
                # P2 has a versioned independent configuration schema, with
                # camera as a model string. Never treat it as the P0 pending
                # camera mapping or silently turn the pending files runnable.
                if not isinstance(cfg, dict) or cfg.get("board") != "pynq_z2" or cfg.get("part") != "xc7z020clg400-1" or type(cfg.get("hardware_confirmed")) is not bool:
                    errors.append(f"invalid P2 configuration identity: {path.name}")
                if path.name != "p2_closeout_reference.yaml" and cfg.get("hardware_confirmed") is not False:
                    errors.append(f"pending P2 config became confirmed: {path.name}")
                continue
            cam = cfg["camera"]
            pending = [cam["camera_actual_width"], cam["camera_actual_height"], cam["pclk_polarity"], cam["href_polarity"], cam["vsync_polarity"], *cam["pins"].values()]
            if cfg["mode"] != "hardware_pending" or cfg["disabled"] is not True or cfg["confirmed"] is not False or any(value is not None for value in pending):
                errors.append(f"unconfirmed hardware template became runnable: {path.name}")
        checks.append("hardware_templates_disabled_and_null")
        snapshots = check_snapshot_integrity(root)
        errors += list(snapshots.errors)
        checks += list(snapshots.checks)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    return CheckResult(not errors, tuple(checks), tuple(errors))
