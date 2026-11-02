"""Read-only hardware scope checks for bounded R31 DDR-to-PL replay.

This is an application-level cooperative guard, not structural isolation. It
does not write camera, capture, motor, or motion-control registers, and it
cannot prevent a different process from accessing MMIO outside this transport.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from candidate.pfo3_r25 import PFO3Error


_REGISTER_RANGES = {
    ("pfo3_frontend", "S_AXI"): (0x40000000, 0x40000FFF),
    ("pfo3_dma", "S_AXI_LITE"): (0x40430000, 0x4043FFFF),
    ("raw_dma", "S_AXI_LITE"): (0x40400000, 0x4040FFFF),
    # R40 b09: the legacy P2 Profile path and its profile_dma were removed from the PL.
    ("processing_pipeline", "S_AXI_CONTROL"): (0x43C00000, 0x43C0FFFF),
    ("processing_pipeline", "S_AXI_DIAG"): (0x43C10000, 0x43C1FFFF),
    ("motor_controller", "S_AXI"): (0x43C40000, 0x43C4FFFF),
    ("bsg1_guard", "S_AXI"): (0x43C50000, 0x43C5FFFF),
}

# 0x024..0x034: live mode control and counters (camera straight into PFO3)
_CTRL_READS = frozenset((0x00, 0x04, 0x08, 0x14, 0x1C, 0x24, 0x28, 0x2C, 0x30, 0x34, 0x30c, 0x310, 0x31c, 0x320))
_CTRL_WRITES = frozenset((0x10, 0x24)) | frozenset(range(0x100, 0x200, 4)) | frozenset(range(0x300, 0x320, 4)) | frozenset(range(0x340, 0x360, 4))
_DMA_READS = frozenset((0x00, 0x04, 0x30, 0x34))
_DMA_WRITES = frozenset((0x00, 0x08, 0x0C, 0x10, 0x14,
                         0x30, 0x38, 0x3C, 0x40, 0x44))


def _u32_offset(offset: int) -> int:
    if type(offset) is not int or offset < 0 or offset >= 4096 or offset & 3:
        raise PFO3Error("replay MMIO offset must be aligned within a 4 KiB register window")
    return offset


class GuardedMMIO:
    """Narrow read/write view around one MMIO object."""

    __slots__ = ("_raw", "name", "_reads", "_writes")

    def __init__(self, raw, name: str, *, reads, writes=()):
        self._raw = raw
        self.name = str(name)
        self._reads = frozenset(reads)
        self._writes = frozenset(writes)

    def read(self, offset: int) -> int:
        offset = _u32_offset(offset)
        if offset not in self._reads:
            raise PFO3Error(f"replay scope forbids {self.name} register read 0x{offset:03x}")
        return int(self._raw.read(offset)) & 0xFFFFFFFF

    def write(self, offset: int, value: int) -> None:
        offset = _u32_offset(offset)
        if offset not in self._writes:
            raise PFO3Error(f"replay scope forbids {self.name} register write 0x{offset:03x}")
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise PFO3Error("replay MMIO write value must be uint32")
        self._raw.write(offset, value)


class ReadOnlyMMIO(GuardedMMIO):
    def write(self, offset: int, value: int) -> None:
        offset = _u32_offset(offset)
        raise PFO3Error(f"replay scope forbids writes to read-only {self.name} status MMIO")


class PFO3ReplayScopeR31:
    # R41 live: the camera runs between PFO3 frames (see PFO3LiveScopeR41).
    live_camera = False
    # R41 pipelined live: camera capture may run while a PFO3 frame is in flight.
    camera_overlap = False

    """Enforce the agreed PFO3-only replay contract through read-only checks.

    The status snapshots are observations, not a hardware interlock. A caller
    must keep the cooperative board lock for the entire replay session.
    """

    def __init__(self, overlay, hardware_lock, mmio_factory, *, ctrl_raw, dma_raw, hwh_path):
        if not getattr(hardware_lock, "acquired", False):
            raise PFO3Error("R31 replay scope requires the cooperative hardware lock")
        self.lock = hardware_lock
        self._validate_hwh_ranges(hwh_path)
        ip_dict = getattr(overlay, "ip_dict", None)
        if not isinstance(ip_dict, dict):
            raise PFO3Error("R31 replay scope requires the loaded overlay IP map")
        for name, expected in (
            ("pfo3_frontend", (0x40000000,)), ("pfo3_dma", (0x40430000,)),
            ("raw_dma", (0x40400000,)),
            # PYNQ collapses a multi-interface IP into one ip_dict entry.
            # Both CONTROL and DIAG are legal representatives, but the HWH
            # check above still independently requires BOTH exact ranges.
            # Status MMIO below uses the verified interface addresses rather
            # than this lossy representative address.
            ("processing_pipeline", (0x43C00000, 0x43C10000)),
            ("motor_controller", (0x43C40000,)), ("bsg1_guard", (0x43C50000,)),
        ):
            try:
                actual = int(ip_dict[name]["phys_addr"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PFO3Error("R31 replay scope missing overlay IP address: " + name) from exc
            if actual not in expected:
                raise PFO3Error(f"R31 replay scope address mismatch for {name}")

        self.ctrl = GuardedMMIO(ctrl_raw, "pfo3_frontend", reads=_CTRL_READS, writes=_CTRL_WRITES)
        self.dma = GuardedMMIO(dma_raw, "pfo3_dma", reads=_DMA_READS, writes=_DMA_WRITES)
        self.raw_dma = ReadOnlyMMIO(mmio_factory(0x40400000, 4096), "raw_dma", reads=(0x30, 0x34))
        self.pipeline = ReadOnlyMMIO(mmio_factory(0x43C00000, 4096), "processing_pipeline",
                                     reads=(0x04,))
        self.diag = ReadOnlyMMIO(mmio_factory(0x43C10000, 4096), "camera_diagnostics",
                                 reads=(0x08, 0x3C, 0x84))
        self.motor = ReadOnlyMMIO(mmio_factory(0x43C40000, 4096), "motor_controller", reads=(0x04,))
        self.guard = ReadOnlyMMIO(mmio_factory(0x43C50000, 4096), "bsg1_guard", reads=(0x04,))

    @staticmethod
    def _validate_hwh_ranges(hwh_path) -> None:
        try:
            root = ET.parse(hwh_path).getroot()
        except (OSError, ET.ParseError, TypeError) as exc:
            raise PFO3Error("R31 replay scope cannot verify HWH register ranges") from exc
        actual = set()
        for row in root.findall(".//MEMRANGE"):
            if row.get("MEMTYPE") != "REGISTER":
                continue
            key = (row.get("INSTANCE"), row.get("SLAVEBUSINTERFACE"))
            try:
                bounds = (int(row.get("BASEVALUE"), 0), int(row.get("HIGHVALUE"), 0))
            except (TypeError, ValueError):
                continue
            if key in _REGISTER_RANGES:
                actual.add((key, bounds))
        expected = {(key, bounds) for key, bounds in _REGISTER_RANGES.items()}
        if actual != expected:
            raise PFO3Error("R31 replay scope HWH register-map contract mismatch")

    def assert_safe(self, stage: str, *, require_pfo3_idle: bool = True) -> dict:
        """Read and validate replay scope without issuing control writes."""
        if not getattr(self.lock, "acquired", False):
            raise PFO3Error("R31 replay scope lost cooperative hardware lock")
        abi = (self.ctrl.read(0x00), self.ctrl.read(0x04), self.ctrl.read(0x08))
        if abi != (0x57334650, 0x35000, 31):
            raise PFO3Error("R31 replay scope PFO3 ABI mismatch")

        pfo3_status = self.ctrl.read(0x14)
        pfo3_mm2s_cr, pfo3_mm2s_sr = self.dma.read(0x00), self.dma.read(0x04)
        pfo3_s2mm_cr, pfo3_s2mm_sr = self.dma.read(0x30), self.dma.read(0x34)
        raw_cr, raw_sr = self.raw_dma.read(0x30), self.raw_dma.read(0x34)
        pipeline_status = self.pipeline.read(0x04)
        ready_status = self.diag.read(0x08)
        camera_control = self.diag.read(0x3C)
        monitor_status = self.diag.read(0x84)
        motor_status = self.motor.read(0x04)
        guard_status = self.guard.read(0x04)

        failures = []
        if require_pfo3_idle and pfo3_status & 1:
            failures.append("PFO3 core is not idle")
        if require_pfo3_idle:
            for name, control, status in (("PFO3 MM2S", pfo3_mm2s_cr, pfo3_mm2s_sr),
                                          ("PFO3 S2MM", pfo3_s2mm_cr, pfo3_s2mm_sr)):
                if control & 1:
                    failures.append(name + " DMA is still running")
                if not status & 1:
                    failures.append(name + " DMA is not halted")
        for name, control, status in (("raw", raw_cr, raw_sr),):
            if self.camera_overlap:
                continue
            if self.live_camera:
                # Simple-mode S2MM keeps RS=1 after its transfer; only an unfinished
                # transfer (running and not idle) may overlap a PFO3 frame.
                if control & 1 and not status & 2:
                    failures.append(name + " capture DMA transfer is in flight")
                continue
            if control & 1:
                failures.append(name + " capture DMA is armed")
            if not status & 1:
                failures.append(name + " capture DMA is not halted")
        if not camera_control & 1 and not self.live_camera:
            failures.append("camera reset is not asserted")
        if monitor_status & ((1 << 0) | (1 << 4)):
            failures.append("camera signal monitor is busy or has a pending command")
        # control status bit 2 is packet formatter activity. Bit 0 (core_busy)
        # also includes !camera_ready, which is expected while reset is held.
        if pipeline_status & (1 << 2) and not self.camera_overlap:
            failures.append("legacy packet formatter is active")
        # READY bit 2 means the legacy capture core is busy or system reset is
        # asserted; record it but do not reject it when CAM_CTRL holds reset.
        if not camera_control & 1 and (pipeline_status & 1 or ready_status & 4) and not self.camera_overlap:
            failures.append("legacy capture core is busy while camera reset is released")
        if motor_status & ((1 << 0) | (1 << 1) | (1 << 6)):
            failures.append("motor is busy, armed, or stepping")
        if not motor_status & (1 << 7):
            failures.append("motor enable_n is not inactive")
        if guard_status & ((1 << 0) | (1 << 3) | (1 << 4)):
            failures.append("motion guard is active, motor-busy, or stepping")
        if not guard_status & (1 << 5):
            failures.append("motion guard output is not inactive")
        if failures:
            raise PFO3Error("R31 PFO3 replay scope rejected at " + str(stage) + ": " + "; ".join(failures))

        return {
            "stage": str(stage),
            "status": "read_only_preflight_passed",
            "application_level_guard": True,
            "structural_isolation_proven": False,
            "hardware_writes": 0,
            "pfo3_abi": list(abi),
            "pfo3_status": pfo3_status,
            "pfo3_mm2s_control": pfo3_mm2s_cr,
            "pfo3_mm2s_status": pfo3_mm2s_sr,
            "pfo3_s2mm_control": pfo3_s2mm_cr,
            "pfo3_s2mm_status": pfo3_s2mm_sr,
            "raw_dma_control": raw_cr,
            "raw_dma_status": raw_sr,
            "profile_dma_control": None,
            "profile_dma_status": None,
            "legacy_pipeline_status": pipeline_status,
            "legacy_ready_status": ready_status,
            "camera_control": camera_control,
            "signal_monitor_status": monitor_status,
            "motor_status": motor_status,
            "motion_guard_status": guard_status,
        }


class PFO3LiveScopeR41(PFO3ReplayScopeR31):
    """Live camera between PFO3 frames: raw capture finished, PFO3 idle, motor still idle."""
    live_camera = True


class PFO3LiveOverlapScopeR41(PFO3LiveScopeR41):
    """Pipelined live: the camera path (raw DMA, capture core) runs concurrently with PFO3.
    PFO3 idle/DMA halted, the ABI and the idle motor/guard checks are unchanged."""
    camera_overlap = True
