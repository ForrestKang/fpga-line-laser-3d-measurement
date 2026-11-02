"""Strict YAML configuration loader for the software contract project.

Configuration describes an offline camera-frame contract only. Board wiring
records live under ``board/configs`` and are deliberately not runtime configs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .generated import XC_FRACTION_BITS, SYSTEM_CLOCK_HZ


class ConfigError(ValueError):
    """Raised when configuration syntax, shape, or values are invalid."""


@dataclass(frozen=True, slots=True)
class SystemConfig:
    schema_version: int
    mode: str
    pixel_format: str
    frame_width: int
    frame_height: int
    measured_width_confirmed: bool
    roi_x0: int
    roi_y0: int
    roi_x1: int
    roi_y1: int
    centroid_axis: str
    threshold_dn: int
    min_width_px: int
    max_width_px: int
    min_sum_dn: int
    min_peak_dn: int
    fractional_bits: int
    system_clock_hz: int
    camera_timing_budget_hz: int
    camera_frequency_measured: bool
    distance_unit: str
    angle_unit: str


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that reports duplicate mapping keys."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConfigError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise ConfigError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


_TOP_KEYS = {"schema_version", "mode", "camera", "roi", "centroid", "clock", "units"}
_NESTED_KEYS = {
    "camera": {"pixel_format", "frame_width", "frame_height", "measured_width_confirmed"},
    "roi": {"x0", "y0", "x1", "y1"},
    "centroid": {
        "axis", "threshold_dn", "min_width_px", "max_width_px", "min_sum_dn",
        "min_peak_dn", "fractional_bits",
    },
    "clock": {"system_hz", "camera_timing_budget_hz", "camera_frequency_measured"},
    "units": {"distance", "angle"},
}


def _mapping(value: Any, name: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a YAML mapping")
    if any(not isinstance(k, str) for k in value):
        raise ConfigError(f"{name} field names must be strings")
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise ConfigError(f"{name} is missing required fields: {', '.join(sorted(missing))}")
    if extra:
        raise ConfigError(f"{name} has unknown fields: {', '.join(sorted(extra))}")
    return value


def _int(value: Any, name: str) -> int:
    # bool is a subclass of int in Python, but is never a valid hardware value.
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer (booleans are not accepted)")
    return value


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be true or false")
    return value


def load_config(path: str | Path) -> SystemConfig:
    """Load one strict YAML configuration and return its immutable model.

    Parsing checks the complete schema and scalar types. Call
    :func:`validate_config` directly when checking an in-memory replacement.
    This function validates an offline config before returning it. Board and
    camera wiring templates intentionally use a separate schema.
    """
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            raw = yaml.load(stream, Loader=_UniqueKeyLoader)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {source}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {source}: {exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"configuration is not valid UTF-8: {source}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc
    if raw is None:
        raise ConfigError("configuration is empty")
    root = _mapping(raw, "configuration", _TOP_KEYS)
    nested = {name: _mapping(root[name], name, fields) for name, fields in _NESTED_KEYS.items()}

    camera = nested["camera"]
    roi = nested["roi"]
    centroid = nested["centroid"]
    clock = nested["clock"]
    units = nested["units"]
    mode = root["mode"]
    if not isinstance(mode, str):
        raise ConfigError("mode must be a string")
    pixel_format = camera["pixel_format"]
    axis = centroid["axis"]
    distance_unit = units["distance"]
    angle_unit = units["angle"]
    if not all(isinstance(v, str) for v in (pixel_format, axis, distance_unit, angle_unit)):
        raise ConfigError("pixel_format, axis, and units must be strings")

    result = SystemConfig(
        schema_version=_int(root["schema_version"], "schema_version"),
        mode=mode,
        pixel_format=pixel_format,
        frame_width=_int(camera["frame_width"], "camera.frame_width"),
        frame_height=_int(camera["frame_height"], "camera.frame_height"),
        measured_width_confirmed=_bool(camera["measured_width_confirmed"], "camera.measured_width_confirmed"),
        roi_x0=_int(roi["x0"], "roi.x0"),
        roi_y0=_int(roi["y0"], "roi.y0"),
        roi_x1=_int(roi["x1"], "roi.x1"),
        roi_y1=_int(roi["y1"], "roi.y1"),
        centroid_axis=axis,
        threshold_dn=_int(centroid["threshold_dn"], "centroid.threshold_dn"),
        min_width_px=_int(centroid["min_width_px"], "centroid.min_width_px"),
        max_width_px=_int(centroid["max_width_px"], "centroid.max_width_px"),
        min_sum_dn=_int(centroid["min_sum_dn"], "centroid.min_sum_dn"),
        min_peak_dn=_int(centroid["min_peak_dn"], "centroid.min_peak_dn"),
        fractional_bits=_int(centroid["fractional_bits"], "centroid.fractional_bits"),
        system_clock_hz=_int(clock["system_hz"], "clock.system_hz"),
        camera_timing_budget_hz=_int(clock["camera_timing_budget_hz"], "clock.camera_timing_budget_hz"),
        camera_frequency_measured=_bool(clock["camera_frequency_measured"], "clock.camera_frequency_measured"),
        distance_unit=distance_unit,
        angle_unit=angle_unit,
    )
    validate_config(result)
    return result


def validate_config(config: SystemConfig) -> None:
    """Validate that *config* is a supported, runnable offline frame setup.

    This API intentionally rejects hardware-pending records: actual sensor
    dimensions, active polarities, pin mapping, and measured camera timing must
    come from the board bring-up process, never from offline defaults.
    """
    if not isinstance(config, SystemConfig):
        raise ConfigError("config must be a SystemConfig returned by load_config")
    integer_fields = (
        "schema_version", "frame_width", "frame_height", "roi_x0", "roi_y0",
        "roi_x1", "roi_y1", "threshold_dn", "min_width_px", "max_width_px",
        "min_sum_dn", "min_peak_dn", "fractional_bits", "system_clock_hz",
        "camera_timing_budget_hz",
    )
    boolean_fields = ("measured_width_confirmed", "camera_frequency_measured")
    string_fields = ("mode", "pixel_format", "centroid_axis", "distance_unit", "angle_unit")
    for name in integer_fields:
        if type(getattr(config, name)) is not int:
            raise ConfigError(f"{name} must be an integer (booleans are not accepted)")
    for name in boolean_fields:
        if type(getattr(config, name)) is not bool:
            raise ConfigError(f"{name} must be true or false")
    for name in string_fields:
        if not isinstance(getattr(config, name), str):
            raise ConfigError(f"{name} must be a string")
    if config.schema_version != 1:
        raise ConfigError(f"unsupported schema_version: {config.schema_version}")
    if config.mode != "offline":
        raise ConfigError(
            f"mode {config.mode!r} is not runnable; hardware settings remain unconfirmed"
        )
    if config.pixel_format != "gray8":
        raise ConfigError("camera.pixel_format must be gray8 (one 8-bit pixel per sample)")
    if (config.frame_width, config.frame_height) not in {(640, 480), (752, 480)}:
        raise ConfigError("offline frame size must be 640x480 or 752x480")
    if config.measured_width_confirmed:
        raise ConfigError("offline width cannot be marked as measured hardware evidence")
    if not (0 <= config.roi_x0 < config.roi_x1 <= config.frame_width):
        raise ConfigError("ROI x bounds must satisfy 0 <= x0 < x1 <= frame_width")
    if not (0 <= config.roi_y0 < config.roi_y1 <= config.frame_height):
        raise ConfigError("ROI y bounds must satisfy 0 <= y0 < y1 <= frame_height")
    if config.centroid_axis != "row":
        raise ConfigError("centroid.axis must be row in the P0 physical-layout contract")
    if not 0 <= config.threshold_dn <= 255:
        raise ConfigError("centroid.threshold_dn must be in [0, 255]")
    if config.min_width_px < 1 or config.min_width_px > config.max_width_px:
        raise ConfigError("centroid width limits must satisfy 1 <= min_width_px <= max_width_px")
    if config.max_width_px > config.roi_x1 - config.roi_x0:
        raise ConfigError("centroid.max_width_px cannot exceed the ROI width")
    if not 0 <= config.min_sum_dn <= (config.roi_x1 - config.roi_x0) * 255:
        raise ConfigError("centroid.min_sum_dn exceeds the possible ROI accumulator range")
    if not 0 <= config.min_peak_dn <= 255:
        raise ConfigError("centroid.min_peak_dn must be in [0, 255]")
    if config.fractional_bits != XC_FRACTION_BITS:
        raise ConfigError("centroid.fractional_bits must be 12 (Q10.12 contract)")
    if config.system_clock_hz != SYSTEM_CLOCK_HZ:
        raise ConfigError("clock.system_hz must be 100 MHz")
    if config.camera_timing_budget_hz not in {48_000_000, 60_000_000}:
        raise ConfigError("clock.camera_timing_budget_hz must be 48 MHz or 60 MHz")
    if config.camera_frequency_measured:
        raise ConfigError("P0 offline configs cannot claim a measured camera clock")
    if config.distance_unit != "mm" or config.angle_unit != "rad":
        raise ConfigError("units must be distance=mm and angle=rad")
