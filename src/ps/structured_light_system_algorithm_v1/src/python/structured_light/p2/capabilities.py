"""Capability declarations and checks shared by Mock and hardware transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import CapabilityError


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Features proven by one transport/overlay.

    ``hardware`` describes where the transport runs.  It is never inferred
    from a filename or from a successful Mock operation.
    """

    raw_dma: bool = False
    profile_dma: bool = False
    camera_sccb: bool = False
    camera_diagnostics: bool = False
    motion: bool = False
    hardware: bool = False
    overlay_name: str = "mock"
    source: str = "offline"

    @classmethod
    def offline_mock(cls, *, profile: bool = True, raw: bool = True) -> "Capabilities":
        return cls(raw_dma=raw, profile_dma=profile, hardware=False, overlay_name="mock", source="mock")

    @classmethod
    def replay(cls, *, profile: bool = True, raw: bool = True) -> "Capabilities":
        return cls(raw_dma=raw, profile_dma=profile, hardware=False, overlay_name="replay", source="replay")

    def require(self, *features: str) -> None:
        for feature in features:
            if not isinstance(feature, str) or not hasattr(self, feature) or not bool(getattr(self, feature)):
                raise CapabilityError(
                    f"transport {self.overlay_name!r} does not provide capability {feature!r}"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_dma": self.raw_dma,
            "profile_dma": self.profile_dma,
            "camera_sccb": self.camera_sccb,
            "camera_diagnostics": self.camera_diagnostics,
            "motion": self.motion,
            "hardware": self.hardware,
            "overlay_name": self.overlay_name,
            "source": self.source,
        }


def require_capabilities(capabilities: Capabilities, features: Iterable[str]) -> None:
    capabilities.require(*tuple(features))


__all__ = ["Capabilities", "require_capabilities", "CapabilityError"]
