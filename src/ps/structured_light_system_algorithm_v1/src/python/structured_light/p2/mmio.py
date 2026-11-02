"""Strict offline MMIO model for the P2 control and diagnostic windows."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .contract import (
    CONFIG_REGS, CORE_BASE, DIAG_OFFSETS, DIAGNOSTICS_BASE, IRQ_MASK, READ_ONLY_REGS,
    REG_PROFILE_COUNT, REG_PROFILE_ID, REG_STEPS_PER_PROFILE,
    REGISTER_NAMES, REGISTER_RESETS, BoardContract, load_board_contract,
)
from .errors import BusyError, ConfigurationError, MMIOError
from .types import DiagnosticRow, DiagnosticsSnapshot


def _check_u32(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF_FFFF:
        raise MMIOError(f"{name} must be an unsigned 32-bit integer")
    return value


def _merge_wstrb(old: int, new: int, wstrb: int) -> int:
    mask = 0
    for lane in range(4):
        if wstrb & (1 << lane):
            mask |= 0xFF << (lane * 8)
    return ((old & ~mask) | (new & mask)) & 0xFFFF_FFFF


class MockMMIO:
    """A strict 32-bit AXI-Lite register model.

    The model deliberately rejects unknown addresses and writes to read-only
    registers.  It is used by both the Mock transport and host contract tests;
    it does not pretend to be a live FPGA bus.
    """

    def __init__(
        self,
        *,
        frame_width: int = 640,
        frame_height: int = 480,
        contract: BoardContract | None = None,
        epoch: int = 0,
    ) -> None:
        if type(frame_width) is not int or frame_width <= 0:
            raise ConfigurationError("frame_width must be a positive integer")
        if type(frame_height) is not int or frame_height <= 0 or frame_height > 480:
            raise ConfigurationError("frame_height must be an integer in 1..480")
        if type(epoch) is not int or epoch < 0:
            raise ConfigurationError("epoch must be nonnegative")
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.contract = contract or load_board_contract()
        self.epoch = epoch
        self._regs = dict(REGISTER_RESETS)
        # The contract's 640x480 reset is the default board mode.  Offline
        # small-frame fixtures still need a valid in-frame ROI.
        self._regs[16] = (frame_height << 16) | frame_width
        self._timestamp_counter = 0
        self._timestamp_latched = 0
        self._mechanical_counter = 0
        self._mechanical_latched = 0
        self._busy = False
        self._scan_done = False
        self._packer_busy = False
        self._motion_state = 0
        self._error_flags = 0
        self._profile_id = 0
        self._fifo_level = 0
        self._next_request_id = 0
        self._last_request_id: int | None = None
        self._irq_status = 0
        self._diag_words: dict[int, int] = {}
        self._rows: tuple[DiagnosticRow, ...] = ()
        self._init_diagnostics()

    @staticmethod
    def _check_offset(offset: int) -> int:
        if type(offset) is not int or offset % 4:
            raise MMIOError("MMIO address must be a 32-bit word-aligned integer")
        return offset

    def _core_offset(self, address: int) -> int:
        address = self._check_offset(address)
        if self.contract.control_base <= address < self.contract.control_base + self.contract.window:
            offset = address - self.contract.control_base
        elif address in REGISTER_NAMES:
            offset = address
        else:
            raise MMIOError(f"unknown control register address 0x{address:X}")
        if offset not in REGISTER_NAMES:
            raise MMIOError(f"unknown control register offset 0x{offset:X}")
        return offset

    def _diag_offset(self, address: int) -> int:
        address = self._check_offset(address)
        if self.contract.diagnostics_base <= address < self.contract.diagnostics_base + self.contract.window:
            return address - self.contract.diagnostics_base
        if address in DIAG_OFFSETS.values() or address >= DIAG_OFFSETS["row_table"]:
            return address
        raise MMIOError(f"unknown diagnostics address 0x{address:X}")

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def request_id(self) -> int | None:
        return self._last_request_id

    @property
    def timestamp(self) -> int:
        return self._timestamp_counter & 0xFFFF_FFFF_FFFF_FFFF

    @property
    def irq_status(self) -> int:
        return self._irq_status

    def _init_diagnostics(self) -> None:
        self._diag_words = {
            DIAG_OFFSETS["version"]: 0x0001_0000,
            DIAG_OFFSETS["capabilities"]: 0x0000_001F,
            DIAG_OFFSETS["ready"]: 1,
            DIAG_OFFSETS["pclk_hz"]: 0,
            DIAG_OFFSETS["width"]: self.frame_width,
            DIAG_OFFSETS["height"]: self.frame_height,
            DIAG_OFFSETS["last_line"]: 0,
            DIAG_OFFSETS["last_frame"]: 0,
            DIAG_OFFSETS["frame_count"]: 0,
            DIAG_OFFSETS["camera_device_address"]: 0x48,
            DIAG_OFFSETS["camera_register_address"]: 0,
            DIAG_OFFSETS["camera_write_data"]: 0,
            DIAG_OFFSETS["camera_read_data"]: 0,
            DIAG_OFFSETS["camera_command"]: 0,
            DIAG_OFFSETS["camera_status"]: 0,
            DIAG_OFFSETS["camera_control"]: 1,
            DIAG_OFFSETS["abort"]: 0,
            DIAG_OFFSETS["epoch"]: self.epoch,
            DIAG_OFFSETS["request_id"]: 0,
            DIAG_OFFSETS["row_count"]: 0,
            DIAG_OFFSETS["error"]: 0,
            DIAG_OFFSETS["w1c"]: 0,
        }

    def _status_word(self) -> int:
        return (
            int(self._busy)
            | (int(self._scan_done) << 1)
            | (int(self._packer_busy) << 2)
            | ((self._motion_state & 0xF) << 4)
        )

    def read(self, address: int) -> int:
        """Read a control register using either its offset or absolute address."""

        offset = self._core_offset(address)
        if offset == 4:
            return self._status_word()
        if offset == 48:
            return self._error_flags & 0xFFFF_FFFF
        if offset == 52:
            return self._regs.get(offset, 0) & 0xFFFF_FFFF
        if offset == 56:
            self._timestamp_latched = self.timestamp
            return self._timestamp_latched & 0xFFFF_FFFF
        if offset == 60:
            return (self._timestamp_latched >> 32) & 0xFFFF_FFFF
        if offset == 96:
            return self._profile_id & 0xFFFF_FFFF
        if offset == 100:
            return self._irq_status & IRQ_MASK
        if offset == 108:
            self._mechanical_latched = self._mechanical_counter & 0xFFFF_FFFF_FFFF_FFFF
            return self._mechanical_latched & 0xFFFF_FFFF
        if offset == 112:
            return (self._mechanical_latched >> 32) & 0xFFFF_FFFF
        if offset == 116:
            return self._fifo_level & 0xFFFF_FFFF
        return self._regs.get(offset, 0) & 0xFFFF_FFFF

    # Common aliases used by MMIO adapters and tests.
    read32 = read

    def write(self, address: int, value: int, wstrb: int = 0xF, *, strb: int | None = None) -> None:
        """Perform one strict AXI-Lite write with byte enables."""

        if strb is not None:
            if wstrb != 0xF:
                raise MMIOError("use either wstrb or strb, not both")
            wstrb = strb
        value = _check_u32(value, "value")
        if type(wstrb) is not int or not 0 <= wstrb <= 0xF:
            raise MMIOError("wstrb must be a four-bit integer")
        offset = self._core_offset(address)
        if offset in READ_ONLY_REGS:
            raise MMIOError(f"register {REGISTER_NAMES[offset]} is read-only")
        if offset == 100:
            # W1C is intentionally lane-aware.  An event generated in the
            # same model step is applied after this clear and therefore wins.
            clear_mask = value & sum(0xFF << (8 * n) for n in range(4) if wstrb & (1 << n))
            self._irq_status &= ~(clear_mask & IRQ_MASK)
            return
        if offset == 0:
            command_mask = value & sum(0xFF << (8 * n) for n in range(4) if wstrb & (1 << n))
            command = command_mask & 0xF
            if command == 0:
                return
            if command & (command - 1):
                raise MMIOError("CONTROL accepts one command pulse per write")
            if command == 1:
                self.accept_request()
            elif command == 2:
                self.stop_capture()
            elif command == 4:
                if self._busy:
                    raise BusyError("HOME rejected while capture is busy")
                raise ConfigurationError("HOME is unavailable in the P2 static profile overlay")
            elif command == 8:
                self.clear()
            return
        if self._busy and offset in CONFIG_REGS:
            raise BusyError(f"configuration write to {REGISTER_NAMES[offset]} rejected while busy")
        old = self._regs.get(offset, 0)
        self._regs[offset] = _merge_wstrb(old, value, wstrb)

    write32 = write

    def read_diag(self, address: int) -> int:
        offset = self._diag_offset(address)
        if offset >= DIAG_OFFSETS["row_table"]:
            byte_offset = offset - DIAG_OFFSETS["row_table"]
            row_index, within_row = divmod(byte_offset, 0x40)
            word_index = within_row // 4
            return self._row_word(row_index, word_index)
        return self._diag_words.get(offset, 0) & 0xFFFF_FFFF

    def write_diag(self, address: int, value: int, wstrb: int = 0xF) -> None:
        value = _check_u32(value, "value")
        if type(wstrb) is not int or not 0 <= wstrb <= 0xF:
            raise MMIOError("wstrb must be a four-bit integer")
        offset = self._diag_offset(address)
        if offset >= DIAG_OFFSETS["row_table"]:
            raise MMIOError("diagnostic row table is read-only")
        writable = {DIAG_OFFSETS["camera_device_address"], DIAG_OFFSETS["camera_register_address"],
                    DIAG_OFFSETS["camera_write_data"], DIAG_OFFSETS["camera_command"],
                    DIAG_OFFSETS["camera_control"], DIAG_OFFSETS["abort"], DIAG_OFFSETS["w1c"]}
        if offset not in writable:
            raise MMIOError(f"diagnostic register 0x{offset:X} is read-only")
        old = self._diag_words.get(offset, 0)
        merged = _merge_wstrb(old, value, wstrb)
        if offset == DIAG_OFFSETS["abort"] and merged:
            self.stop_capture()
            self._diag_words[offset] = 0
        elif offset == DIAG_OFFSETS["w1c"]:
            self._error_flags &= ~merged
            self._diag_words[DIAG_OFFSETS["error"]] &= ~merged
        else:
            self._diag_words[offset] = merged

    def tick(self, cycles: int = 1) -> None:
        if type(cycles) is not int or cycles < 0:
            raise MMIOError("cycles must be a nonnegative integer")
        self._timestamp_counter = (self._timestamp_counter + cycles) & 0xFFFF_FFFF_FFFF_FFFF

    def set_mechanical_counter(self, value: int) -> None:
        if type(value) is not int or not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF:
            raise MMIOError("mechanical counter must be uint64")
        self._mechanical_counter = value

    def accept_request(self) -> int:
        if self._busy:
            raise BusyError("START rejected while capture is busy")
        if self._regs.get(REG_PROFILE_COUNT, 1) != 1:
            raise ConfigurationError("P2 static capture requires PROFILE_COUNT=1")
        if self._regs.get(REG_STEPS_PER_PROFILE, 0) != 0:
            raise ConfigurationError("P2 static capture requires STEPS_PER_PROFILE=0")
        self._busy = True
        self._scan_done = False
        self._packer_busy = True
        self._error_flags = 0
        self._timestamp_latched = self.timestamp
        request_id = self._next_request_id & 0xFFFF_FFFF
        self._next_request_id = (self._next_request_id + 1) & 0xFFFF_FFFF
        self._last_request_id = request_id
        self._regs[REG_PROFILE_ID] = request_id
        self._diag_words[DIAG_OFFSETS["epoch"]] = self.epoch
        self._diag_words[DIAG_OFFSETS["request_id"]] = request_id
        return request_id

    def emit_irq(self, mask: int) -> None:
        """Record an event after a W1C write, preserving same-cycle events."""

        if type(mask) is not int or mask < 0 or mask & ~IRQ_MASK:
            raise MMIOError("IRQ event mask must be a three-bit value")
        self._irq_status |= mask

    def complete_request(self, *, profile_id: int | None = None, error_flags: int = 0) -> None:
        if not self._busy:
            raise BusyError("cannot complete an idle capture")
        self._busy = False
        self._packer_busy = False
        self._scan_done = error_flags == 0
        self._error_flags = error_flags & 0xFFFF_FFFF
        if profile_id is not None:
            _check_u32(profile_id, "profile_id")
            self._profile_id = profile_id
            self._regs[96] = profile_id
        self.emit_irq(1 if error_flags == 0 else 4)
        if self._scan_done:
            self.emit_irq(2)

    def stop_capture(self) -> None:
        if not self._busy:
            return
        self._busy = False
        self._packer_busy = False
        self._scan_done = False
        self.epoch += 1
        self._diag_words[DIAG_OFFSETS["abort"]] = 1
        self._diag_words[DIAG_OFFSETS["epoch"]] = self.epoch
        self._error_flags |= 1 << 18
        self._diag_words[DIAG_OFFSETS["error"]] |= 1 << 18
        self.emit_irq(4)

    def clear(self) -> None:
        if self._busy:
            raise BusyError("CLEAR is not a capture abort")
        self._scan_done = False
        self._error_flags = 0
        self._irq_status = 0
        self._diag_words[DIAG_OFFSETS["error"]] = 0
        self._diag_words[DIAG_OFFSETS["abort"]] = 0

    def set_fifo_level(self, level: int) -> None:
        _check_u32(level, "fifo level")
        self._fifo_level = level

    def set_diagnostics(
        self,
        rows: Iterable[DiagnosticRow],
        *,
        pclk_hz: int = 0,
        frame_count: int | None = None,
        last_frame: int = 0,
        error: int = 0,
    ) -> None:
        row_tuple = tuple(rows)
        self._rows = row_tuple
        self._diag_words[DIAG_OFFSETS["pclk_hz"]] = pclk_hz & 0xFFFF_FFFF
        self._diag_words[DIAG_OFFSETS["width"]] = self.frame_width
        self._diag_words[DIAG_OFFSETS["height"]] = self.frame_height
        self._diag_words[DIAG_OFFSETS["last_line"]] = row_tuple[-1].row if row_tuple else 0
        self._diag_words[DIAG_OFFSETS["last_frame"]] = last_frame & 0xFFFF_FFFF
        if frame_count is not None:
            self._diag_words[DIAG_OFFSETS["frame_count"]] = frame_count & 0xFFFF_FFFF
        self._diag_words[DIAG_OFFSETS["row_count"]] = len(row_tuple)
        self._diag_words[DIAG_OFFSETS["request_id"]] = self._last_request_id or 0
        self._diag_words[DIAG_OFFSETS["epoch"]] = self.epoch
        self._diag_words[DIAG_OFFSETS["error"]] = error & 0xFFFF_FFFF

    def _row_word(self, row_index: int, word_index: int) -> int:
        if word_index < 0 or word_index >= 16:
            raise MMIOError("diagnostic row word must be in 0..15")
        if row_index < 0 or row_index >= len(self._rows):
            return 0
        row = self._rows[row_index]
        packed = ((row.flags & 0xFFFF) << 8) | (row.quality & 0xFF) | (int(row.valid) << 23)
        values = (row.row, row.xc_fixed, row.s0, row.s1, row.peak, row.width,
                  row.candidate_count, packed, row.diagnostics, row.profile_id)
        return values[word_index] & 0xFFFF_FFFF if word_index < len(values) else 0

    def diagnostics_snapshot(self) -> DiagnosticsSnapshot:
        words = self._diag_words
        return DiagnosticsSnapshot(
            version=words[DIAG_OFFSETS["version"]], capabilities=words[DIAG_OFFSETS["capabilities"]],
            ready=bool(words[DIAG_OFFSETS["ready"]]), pclk_hz=words[DIAG_OFFSETS["pclk_hz"]],
            width=words[DIAG_OFFSETS["width"]], height=words[DIAG_OFFSETS["height"]],
            last_line=words[DIAG_OFFSETS["last_line"]], last_frame=words[DIAG_OFFSETS["last_frame"]],
            frame_count=words[DIAG_OFFSETS["frame_count"]],
            camera_device_address=words[DIAG_OFFSETS["camera_device_address"]],
            camera_register_address=words[DIAG_OFFSETS["camera_register_address"]],
            camera_write_data=words[DIAG_OFFSETS["camera_write_data"]],
            camera_read_data=words[DIAG_OFFSETS["camera_read_data"]],
            camera_command=words[DIAG_OFFSETS["camera_command"]], camera_status=words[DIAG_OFFSETS["camera_status"]],
            camera_control=words[DIAG_OFFSETS["camera_control"]], abort=words[DIAG_OFFSETS["abort"]],
            epoch=words[DIAG_OFFSETS["epoch"]], request_id=words[DIAG_OFFSETS["request_id"]],
            row_count=words[DIAG_OFFSETS["row_count"]], error=words[DIAG_OFFSETS["error"]], rows=self._rows,
        )


__all__ = ["MockMMIO"]
