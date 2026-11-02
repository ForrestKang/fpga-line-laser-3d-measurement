"""Canonical, versioned measured-settling protocol descriptors.

The descriptors are deliberately small and immutable.  They are loaded from
the machine-readable schema-1 contract shipped with this source tree and are
validated against the two supported protocol identities before they become
available to callers.  Receipts must pass through :func:`validate_settle_protocol`
before a descriptor is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


SETTLE_PROTOCOL_SCHEMA_VERSION = 1
LEGACY_SETTLE_PROTOCOL_ID = "legacy_3s_v1"
EXTENDED_SETTLE_PROTOCOL_ID = "extended_6s_v2"


@dataclass(frozen=True, slots=True)
class SettleProtocol:
    """One canonical schedule and its two same-pose late references."""

    schema_version: int
    protocol_id: str
    schedule_ms: tuple[int, ...]
    late_reference_ms: tuple[int, int]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SETTLE_PROTOCOL_SCHEMA_VERSION:
            raise ValueError("unsupported settle protocol schema_version")
        if type(self.protocol_id) is not str or not self.protocol_id:
            raise ValueError("settle protocol_id must be a non-empty string")
        if not self.schedule_ms or any(
            type(value) is not int or value <= 0
            for value in self.schedule_ms
        ):
            raise ValueError("settle protocol schedule_ms must contain positive integers")
        if tuple(sorted(self.schedule_ms)) != self.schedule_ms or len(set(self.schedule_ms)) != len(self.schedule_ms):
            raise ValueError("settle protocol schedule_ms must be strictly increasing")
        if len(self.late_reference_ms) != 2:
            raise ValueError("settle protocol requires exactly two late references")
        if any(type(value) is not int or value not in self.schedule_ms for value in self.late_reference_ms):
            raise ValueError("settle protocol late references must be in schedule_ms")
        if tuple(self.schedule_ms[-2:]) != tuple(self.late_reference_ms):
            raise ValueError("settle protocol late references must be the final two samples")

    @property
    def late_pair_ms(self) -> tuple[int, int]:
        """Compatibility spelling for the final same-pose reference pair."""

        return self.late_reference_ms

    @property
    def schedule(self) -> tuple[int, ...]:
        """Compatibility spelling for the finite sample schedule."""

        return self.schedule_ms

    def as_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-safe canonical descriptor."""

        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "schedule_ms": list(self.schedule_ms),
            "late_reference_ms": list(self.late_reference_ms),
        }


def _contract_path() -> Path:
    return Path(__file__).resolve().parents[4] / "src" / "contracts" / "b_settle_protocols.json"


def _load_contract() -> dict[str, SettleProtocol]:
    path = _contract_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load settle protocol contract {path}: {exc}") from exc
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SETTLE_PROTOCOL_SCHEMA_VERSION
    ):
        raise RuntimeError("settle protocol contract must use schema_version 1")
    records = payload.get("protocols")
    if not isinstance(records, Mapping):
        raise RuntimeError("settle protocol contract protocols must be an object")
    descriptors: dict[str, SettleProtocol] = {}
    for key, record in records.items():
        if not isinstance(key, str) or not isinstance(record, Mapping):
            raise RuntimeError("settle protocol contract contains a malformed descriptor")
        schema_version = record.get("schema_version")
        protocol_id = record.get("protocol_id")
        schedule_ms = record.get("schedule_ms")
        late_reference_ms = record.get("late_reference_ms")
        if (
            type(schema_version) is not int
            or type(protocol_id) is not str
            or type(schedule_ms) is not list
            or type(late_reference_ms) is not list
        ):
            raise RuntimeError(f"settle protocol descriptor {key} has malformed field types")
        descriptor = SettleProtocol(
            schema_version=schema_version,
            protocol_id=protocol_id,
            schedule_ms=tuple(schedule_ms),
            late_reference_ms=tuple(late_reference_ms),
        )
        if descriptor.protocol_id != key:
            raise RuntimeError("settle protocol contract key does not match protocol_id")
        if set(record) != {"schema_version", "protocol_id", "schedule_ms", "late_reference_ms"}:
            raise RuntimeError(f"settle protocol descriptor {key} has noncanonical fields")
        descriptors[key] = descriptor
    expected = {
        LEGACY_SETTLE_PROTOCOL_ID: ((100, 250, 500, 1000, 2000, 2500, 3000), (2500, 3000)),
        EXTENDED_SETTLE_PROTOCOL_ID: ((100, 250, 500, 1000, 3000, 5000, 6000), (5000, 6000)),
    }
    if set(descriptors) != set(expected):
        raise RuntimeError("settle protocol contract does not contain the supported canonical protocols")
    for protocol_id, (schedule, late_pair) in expected.items():
        descriptor = descriptors[protocol_id]
        if descriptor.schedule_ms != schedule or descriptor.late_reference_ms != late_pair:
            raise RuntimeError(f"settle protocol contract descriptor {protocol_id} is not canonical")
    return descriptors


_PROTOCOLS = _load_contract()
SETTLE_PROTOCOL_IDS = tuple(_PROTOCOLS)


def get_settle_protocol(protocol_id: str) -> SettleProtocol:
    """Return the immutable canonical protocol identified by ``protocol_id``."""

    if not isinstance(protocol_id, str) or protocol_id not in _PROTOCOLS:
        raise ValueError(f"unknown settle protocol: {protocol_id!r}")
    return _PROTOCOLS[protocol_id]


def validate_settle_protocol(value: Mapping[str, Any], *, protocol_id: str | None = None) -> SettleProtocol:
    """Validate a persisted descriptor and return its canonical instance."""

    if not isinstance(value, Mapping):
        raise ValueError("settle protocol descriptor must be an object")
    value_id = value.get("protocol_id")
    if not isinstance(value_id, str) or not value_id:
        raise ValueError("settle protocol descriptor protocol_id is missing")
    if protocol_id is not None and value_id != protocol_id:
        raise ValueError("settle protocol descriptor protocol_id does not match the binding")
    canonical = get_settle_protocol(value_id)
    expected = canonical.as_dict()
    if (
        set(value) != set(expected)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != expected["schema_version"]
        or type(value.get("protocol_id")) is not str
        or value.get("protocol_id") != expected["protocol_id"]
        or type(value.get("schedule_ms")) is not list
        or any(type(item) is not int for item in value.get("schedule_ms", ()))
        or value.get("schedule_ms") != expected["schedule_ms"]
        or type(value.get("late_reference_ms")) is not list
        or any(type(item) is not int for item in value.get("late_reference_ms", ()))
        or value.get("late_reference_ms") != expected["late_reference_ms"]
    ):
        raise ValueError("settle protocol descriptor is not canonical")
    return canonical


__all__ = [
    "EXTENDED_SETTLE_PROTOCOL_ID",
    "LEGACY_SETTLE_PROTOCOL_ID",
    "SETTLE_PROTOCOL_IDS",
    "SETTLE_PROTOCOL_SCHEMA_VERSION",
    "SettleProtocol",
    "get_settle_protocol",
    "validate_settle_protocol",
]
