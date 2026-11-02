"""Errors raised by the P2 host acquisition layer.

The exception hierarchy is deliberately independent from PYNQ.  This lets the
same controller report a configuration, timeout, or protocol failure in the
offline Mock/Replay transports and on a board.
"""

from __future__ import annotations


class P2Error(RuntimeError):
    """Base class for host-side P2 failures."""


class ConfigurationError(P2Error, ValueError):
    """A value cannot be represented by the frozen board contract."""


class ContractError(P2Error, ValueError):
    """A file, overlay, or hardware contract is inconsistent."""


class CapabilityError(P2Error):
    """The selected transport cannot perform the requested operation."""


class BusyError(P2Error):
    """A second request or a configuration write was attempted while busy."""


class MMIOError(P2Error, ValueError):
    """An invalid register access or an invalid AXI-Lite write was attempted."""


class DMAError(P2Error):
    """A DMA channel did not arm, complete, or recover correctly."""


class CaptureTimeout(P2Error, TimeoutError):
    """A bounded capture wait expired."""


class CaptureCancelled(P2Error):
    """The in-flight capture was explicitly stopped or reset."""


class ProtocolError(P2Error, ValueError):
    """A completed raw/Profile transfer violates its wire contract."""


class VersionMismatch(ContractError):
    """An overlay, register map, or profile format is incompatible."""


__all__ = [
    "P2Error", "ConfigurationError", "ContractError", "CapabilityError",
    "BusyError", "MMIOError", "DMAError", "CaptureTimeout", "CaptureCancelled",
    "ProtocolError", "VersionMismatch",
]
