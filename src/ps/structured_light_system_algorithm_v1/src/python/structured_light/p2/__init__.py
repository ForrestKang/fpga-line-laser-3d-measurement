"""Host-side P2 acquisition APIs.

The package is safe to import on a PC.  PYNQ is imported lazily only by
``structured_light.p2.pynq_transport.PynqTransport`` when a verified overlay
is explicitly selected.
"""

from .capabilities import Capabilities
from .controller import Controller, P2Controller, StructuredLightController
from .contract import BoardContract, load_board_contract
from .errors import *
from .mmio import MockMMIO
from .pynq_transport import BoundedS2MM, PynqTransport, parse_hwh_addresses, parse_hwh_bases, verify_overlay_artifacts
from .reference import compare_profile, compare_rows, compare_same_frame
from .pair_evidence import export_pair_evidence
from .session import SessionRecorder
from .transport import CaptureTransport, FileReplayTransport, MockTransport, ReplayTransport
from .types import *

__all__ = [
    "Capabilities", "P2Controller", "Controller", "StructuredLightController", "MockMMIO",
    "BoardContract", "load_board_contract",
    "CaptureTransport", "MockTransport", "ReplayTransport", "FileReplayTransport",
    "SessionRecorder", "compare_profile", "compare_rows", "compare_same_frame", "export_pair_evidence",
    "BoundedS2MM", "PynqTransport", "parse_hwh_addresses", "parse_hwh_bases",
    "verify_overlay_artifacts",
]
