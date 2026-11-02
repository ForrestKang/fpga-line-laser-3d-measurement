"""Strict, offline reader for versioned raw gray8 stripe samples."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


WIDTH = 752
HEIGHT = 480
SPLITS = ("development", "validation", "test")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class DatasetError(ValueError):
    """Raised when a sample record or split assignment is not auditable."""


@dataclass(frozen=True, slots=True)
class AcquisitionSetting:
    value: int
    unit: str


@dataclass(frozen=True, slots=True)
class Sample:
    sample_id: str
    record_path: Path
    raw_path: Path
    width: int
    height: int
    pixels: bytes
    sha256: str
    installation_id: str
    session_id: str
    split: str
    exposure: AcquisitionSetting
    gain: AcquisitionSetting
    label_status: str
    label_version: str | None


@dataclass(frozen=True, slots=True)
class SplitReport:
    sample_count: int
    session_count: int
    counts_by_split: tuple[tuple[str, int], ...]
    installation_ids: tuple[str, ...]


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _object(value: Any, name: str, required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DatasetError(f"{name} must be an object")
    unknown = set(value) - required
    missing = required - set(value)
    if unknown:
        raise DatasetError(f"{name} has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise DatasetError(f"{name} is missing field(s): {', '.join(sorted(missing))}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise DatasetError(f"{name} must be an integer >= {minimum}")
    return value


def _setting(value: Any, name: str, *, minimum: int) -> AcquisitionSetting:
    fields = _object(value, name, {"value", "unit"})
    setting_value = _integer(fields["value"], f"{name}.value", minimum=minimum)
    unit = _text(fields["unit"], f"{name}.unit")
    return AcquisitionSetting(setting_value, unit)


def _read_record(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetError(f"sample record is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise DatasetError(f"cannot read sample record {path}: {exc}") from exc
    try:
        value = json.loads(source, object_pairs_hook=_pairs_no_duplicates)
    except DatasetError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DatasetError(f"invalid UTF-8 JSON sample record {path}: {exc}") from exc
    return _object(
        value,
        "record",
        {
            "schema_version",
            "sample_id",
            "raw_gray8",
            "installation_id",
            "session_id",
            "split",
            "acquisition",
            "labels",
        },
    )


def _raw_file(record_path: Path, value: Any) -> tuple[Path, bytes]:
    fields = _object(value, "raw_gray8", {"path", "width", "height", "sha256"})
    raw_name = _text(fields["path"], "raw_gray8.path")
    relative = Path(raw_name)
    if relative.is_absolute() or relative.suffix.lower() != ".gray8":
        raise DatasetError("raw_gray8.path must be a relative .gray8 path")
    record_dir = record_path.parent.resolve()
    try:
        raw_path = (record_dir / relative).resolve(strict=True)
        raw_path.relative_to(record_dir)
    except (OSError, ValueError) as exc:
        raise DatasetError("raw_gray8.path must resolve to a file under the record directory") from exc
    try:
        pixels = raw_path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"cannot read raw gray8 file {raw_path}: {exc}") from exc
    return raw_path, pixels


def load_sample(path: str | Path) -> Sample:
    """Load one record and verify every byte of its 752x480 gray8 image."""

    record_path = Path(path)
    record = _read_record(record_path)
    if _integer(record["schema_version"], "schema_version", minimum=1) != 1:
        raise DatasetError("schema_version must be 1")
    sample_id = _text(record["sample_id"], "sample_id")
    installation_id = _text(record["installation_id"], "installation_id")
    session_id = _text(record["session_id"], "session_id")
    split = _text(record["split"], "split")
    if split not in SPLITS:
        raise DatasetError(f"split must be one of {', '.join(SPLITS)}")

    image_fields = _object(record["raw_gray8"], "raw_gray8", {"path", "width", "height", "sha256"})
    width = _integer(image_fields["width"], "raw_gray8.width", minimum=1)
    height = _integer(image_fields["height"], "raw_gray8.height", minimum=1)
    if (width, height) != (WIDTH, HEIGHT):
        raise DatasetError(f"raw_gray8 dimensions must be {WIDTH}x{HEIGHT}")
    declared_sha = _text(image_fields["sha256"], "raw_gray8.sha256")
    if not _SHA256_RE.fullmatch(declared_sha):
        raise DatasetError("raw_gray8.sha256 must be 64 lowercase hexadecimal characters")
    raw_path, pixels = _raw_file(record_path, record["raw_gray8"])
    expected_size = width * height
    if len(pixels) != expected_size:
        raise DatasetError(f"raw gray8 file has {len(pixels)} bytes; expected {expected_size} bytes")
    actual_sha = hashlib.sha256(pixels).hexdigest()
    if actual_sha != declared_sha:
        raise DatasetError("raw_gray8 sha256 does not match the file bytes")

    acquisition = _object(record["acquisition"], "acquisition", {"exposure", "gain"})
    exposure = _setting(acquisition["exposure"], "acquisition.exposure", minimum=1)
    gain = _setting(acquisition["gain"], "acquisition.gain", minimum=0)
    labels = _object(record["labels"], "labels", {"status", "version"})
    label_status = _text(labels["status"], "labels.status")
    if label_status == "unlabeled":
        if labels["version"] is not None:
            raise DatasetError("labels.version must be null when labels.status is unlabeled")
        label_version = None
    elif label_status == "labeled":
        label_version = _text(labels["version"], "labels.version")
    else:
        raise DatasetError("labels.status must be 'unlabeled' or 'labeled'")

    return Sample(
        sample_id=sample_id,
        record_path=record_path.resolve(),
        raw_path=raw_path,
        width=width,
        height=height,
        pixels=pixels,
        sha256=actual_sha,
        installation_id=installation_id,
        session_id=session_id,
        split=split,
        exposure=exposure,
        gain=gain,
        label_status=label_status,
        label_version=label_version,
    )


def validate_split(
    samples: Iterable[Sample], *, expected_installation_id: str | None = None
) -> SplitReport:
    """Reject session leakage, session identity changes, and raw-frame reuse."""

    if expected_installation_id is not None:
        expected_installation_id = _text(expected_installation_id, "expected_installation_id")
    sample_list = tuple(samples)
    if not sample_list:
        raise DatasetError("split validation requires at least one sample")
    if any(not isinstance(sample, Sample) for sample in sample_list):
        raise DatasetError("all split entries must be Sample objects loaded by load_sample")

    seen_ids: set[str] = set()
    session_assignment: dict[str, tuple[str, str]] = {}
    digest_splits: dict[str, str] = {}
    counts = {name: 0 for name in SPLITS}
    installations: set[str] = set()
    for sample in sample_list:
        if sample.sample_id in seen_ids:
            raise DatasetError(f"duplicate sample_id {sample.sample_id!r}")
        seen_ids.add(sample.sample_id)
        if expected_installation_id is not None and sample.installation_id != expected_installation_id:
            raise DatasetError(
                f"sample {sample.sample_id!r} installation_id {sample.installation_id!r} "
                f"does not match expected {expected_installation_id!r}"
            )
        previous = session_assignment.get(sample.session_id)
        if previous is not None:
            previous_install, previous_split = previous
            if sample.installation_id != previous_install:
                raise DatasetError(f"session {sample.session_id!r} contains multiple installations")
            if sample.split != previous_split:
                raise DatasetError(f"session {sample.session_id!r} appears in multiple splits")
        else:
            session_assignment[sample.session_id] = (sample.installation_id, sample.split)
        prior_split = digest_splits.get(sample.sha256)
        if prior_split is not None and prior_split != sample.split:
            raise DatasetError("identical raw gray8 bytes appear in multiple splits")
        digest_splits[sample.sha256] = sample.split
        counts[sample.split] += 1
        installations.add(sample.installation_id)

    return SplitReport(
        sample_count=len(sample_list),
        session_count=len(session_assignment),
        counts_by_split=tuple((name, counts[name]) for name in SPLITS if counts[name]),
        installation_ids=tuple(sorted(installations)),
    )


__all__ = [
    "AcquisitionSetting",
    "DatasetError",
    "Sample",
    "SplitReport",
    "load_sample",
    "validate_split",
]
