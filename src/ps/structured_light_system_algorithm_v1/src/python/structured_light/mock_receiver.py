"""Small AXI4-Stream-style Profile receiver used by the P1 host tests.

This is a protocol scoreboard, not a DMA driver.  It consumes a 32-bit word
only on ``tvalid and tready``, checks the AXI stability rule while stalled,
requires one TLAST on the final accepted word, and hands the resulting bytes
to the existing SLP2 codec.  It deliberately keeps reset/epoch cancellation
visible instead of treating a partial packet as a valid Profile.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterable, Sequence

from .profile import Profile, ProfileError, decode_profile, encode_profile


MAX_SLP2_PACKET_BYTES = 5792  # 8 + 3*480 little-endian 32-bit words


class MockReceiverError(ValueError):
    """Raised for an AXI protocol violation or malformed completed packet."""


@dataclass(frozen=True, slots=True)
class AxisWord:
    """A snapshot of one source-side AXI word."""

    data: int
    last: bool = False
    valid: bool = True

    def __post_init__(self) -> None:
        if type(self.data) is not int or not 0 <= self.data <= 0xFFFF_FFFF:
            raise MockReceiverError("AXIS data must be an unsigned 32-bit integer")
        if type(self.last) is not bool or type(self.valid) is not bool:
            raise MockReceiverError("AXIS last and valid must be bool")


@dataclass(frozen=True, slots=True)
class CancellationRecord:
    """A reset-cancelled partial packet, retained for test evidence."""

    old_epoch: int
    new_epoch: int
    accepted_words: int
    reason: str


def _uint32(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF_FFFF:
        raise MockReceiverError(f"{name} must be an unsigned 32-bit integer")
    return value


class MockAxisReceiver:
    """Receive and validate one SLP2 packet over a ready/valid stream."""

    def __init__(
        self,
        *,
        frame_width: int = 640,
        frame_height: int = 480,
        max_packet_bytes: int = MAX_SLP2_PACKET_BYTES,
        epoch: int = 0,
    ) -> None:
        if type(frame_width) is not int or frame_width <= 0:
            raise MockReceiverError("frame_width must be positive")
        if type(frame_height) is not int or frame_height <= 0:
            raise MockReceiverError("frame_height must be positive")
        if type(max_packet_bytes) is not int or max_packet_bytes <= 0:
            raise MockReceiverError("max_packet_bytes must be positive")
        if type(epoch) is not int or epoch < 0:
            raise MockReceiverError("epoch must be a nonnegative integer")
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.max_packet_bytes = max_packet_bytes
        self.epoch = epoch
        self.cancellations: list[CancellationRecord] = []
        self.reset_count = 0
        self._words: list[int] = []
        self._stalled: AxisWord | None = None
        self._profile: Profile | None = None
        self._packet: bytes | None = None
        self._reencoded: bytes | None = None

    @property
    def words(self) -> tuple[int, ...]:
        return tuple(self._words)

    @property
    def packet_bytes(self) -> bytes:
        return self._packet if self._packet is not None else b"".join(
            struct.pack("<I", word) for word in self._words
        )

    @property
    def payload(self) -> bytes:
        """Alias for packet_bytes used by simple byte-oriented host code."""

        return self.packet_bytes

    @property
    def profile(self) -> Profile | None:
        return self._profile

    @property
    def decoded_profile(self) -> Profile | None:
        return self._profile

    @property
    def completed(self) -> bool:
        return self._profile is not None

    @property
    def stalled_word(self) -> AxisWord | None:
        return self._stalled

    @property
    def reencoded_packet(self) -> bytes | None:
        return self._reencoded

    @property
    def byte_exact_roundtrip(self) -> bool:
        return self._packet is not None and self._reencoded == self._packet

    @property
    def cancelled_packets(self) -> tuple[CancellationRecord, ...]:
        return tuple(self.cancellations)

    def _check_stall(self, word: AxisWord) -> None:
        if self._stalled is None:
            self._stalled = word
            return
        if word.data != self._stalled.data or word.last != self._stalled.last:
            raise MockReceiverError(
                "AXIS source changed TDATA or TLAST while TVALID was stalled"
            )

    def _consume(self, word: AxisWord) -> None:
        if self._profile is not None:
            raise MockReceiverError("received a word after TLAST completed the packet")
        if word.last and not self._words:
            # A header-only packet has eight words; this check catches a
            # one-word TLAST before the codec emits its more obscure error.
            raise MockReceiverError("TLAST arrived before an SLP2 header")
        self._words.append(word.data)
        if len(self._words) * 4 > self.max_packet_bytes:
            raise MockReceiverError(
                f"packet exceeds configured limit {self.max_packet_bytes} bytes"
            )
        if not word.last:
            return
        payload = b"".join(struct.pack("<I", value) for value in self._words)
        try:
            profile = decode_profile(
                payload,
                frame_width=self.frame_width,
                frame_height=self.frame_height,
            )
        except ProfileError as exc:
            raise MockReceiverError(f"invalid completed SLP2 packet: {exc}") from exc
        if profile.format_version != 2:
            raise MockReceiverError("P1 MockAxisReceiver accepts SLP2 packets only")
        reencoded = encode_profile(profile)
        if reencoded != payload:
            raise MockReceiverError("SLP2 decode/re-encode was not byte-exact")
        self._packet = payload
        self._profile = profile
        self._reencoded = reencoded

    def accept(
        self,
        data: int | AxisWord,
        *,
        valid: bool = True,
        ready: bool = True,
        last: bool = False,
    ) -> bool:
        """Present one source cycle and return whether a word was accepted."""

        if isinstance(data, AxisWord):
            if valid is not True or last is not False:
                raise MockReceiverError("AxisWord already carries valid/last; do not override it")
            valid = data.valid
            last = data.last
            data = data.data
        word = AxisWord(_uint32(data, "data"), bool(last), bool(valid))
        if type(valid) is not bool or type(ready) is not bool or type(last) is not bool:
            raise MockReceiverError("valid, ready, and last must be bool")
        if not valid:
            if self._stalled is not None:
                raise MockReceiverError("AXIS source deasserted TVALID while stalled")
            return False
        if not ready:
            self._check_stall(word)
            return False
        if self._stalled is not None:
            if data != self._stalled.data or last != self._stalled.last:
                raise MockReceiverError("AXIS source changed stalled word before handshake")
            self._stalled = None
        self._consume(word)
        return True

    # Readable aliases used by small host drivers and tests.
    push_word = accept
    receive = accept

    def step(
        self,
        *,
        tdata: int | AxisWord,
        tvalid: bool = True,
        tready: bool = True,
        tlast: bool = False,
    ) -> bool:
        """AXI-named wrapper for callers that model signal names directly."""

        return self.accept(tdata, valid=tvalid, ready=tready, last=tlast)

    @staticmethod
    def _coerce_word(item: AxisWord | int | Sequence[int | bool]) -> AxisWord:
        if isinstance(item, AxisWord):
            return item
        if type(item) is int:
            return AxisWord(item)
        try:
            values = tuple(item)
        except TypeError as exc:
            raise MockReceiverError("feed item must be AxisWord, int, or (data,last)") from exc
        if len(values) == 2:
            data, last = values
            return AxisWord(data, last=last)  # type: ignore[arg-type]
        if len(values) == 3:
            data, valid, last = values
            return AxisWord(data, last=last, valid=valid)  # type: ignore[arg-type]
        raise MockReceiverError("feed tuple must contain (data,last) or (data,valid,last)")

    def feed(
        self,
        words: Iterable[AxisWord | int | Sequence[int | bool]],
        *,
        ready: bool | Sequence[bool] = True,
    ) -> Profile | None:
        """Feed a finite stream, marking the final item TLAST if omitted."""

        items = list(words)
        if type(ready) is bool:
            ready_values = (ready,) * len(items)
        else:
            ready_values = tuple(ready)
            if len(ready_values) != len(items) or any(type(v) is not bool for v in ready_values):
                raise MockReceiverError("ready sequence must match feed length")
        for index, item in enumerate(items):
            word = self._coerce_word(item)
            if index == len(items) - 1 and not word.last:
                word = AxisWord(word.data, last=True, valid=word.valid)
            self.accept(word.data, valid=word.valid, ready=ready_values[index], last=word.last)
        return self._profile

    def reset(self, epoch: int | None = None, *, reason: str = "reset") -> CancellationRecord | None:
        """Cancel a partial packet and clear all stream state for a new epoch."""

        if epoch is None:
            new_epoch = self.epoch + 1
        elif type(epoch) is int and epoch >= 0:
            new_epoch = epoch
        else:
            raise MockReceiverError("epoch must be a nonnegative integer")
        record = None
        if self._profile is None and (self._words or self._stalled is not None):
            record = CancellationRecord(self.epoch, new_epoch, len(self._words), reason)
            self.cancellations.append(record)
        self.epoch = new_epoch
        self.reset_count += 1
        self._words.clear()
        self._stalled = None
        self._profile = None
        self._packet = None
        self._reencoded = None
        return record


def encode_slp2_rows(
    rows: Iterable[object],
    *,
    profile_id: int = 0,
    angle_raw: int = 0,
    timestamp: int = 0,
    error_flags: int = 0,
    frame_width: int = 640,
    frame_height: int = 480,
) -> bytes:
    """Build an SLP2 packet from RowResult-like objects for host tests."""

    from .profile import Profile, ProfilePoint

    points: list[ProfilePoint] = []
    for result in rows:
        if isinstance(result, ProfilePoint):
            points.append(result)
        else:
            try:
                points.append(
                    ProfilePoint(
                        row=int(result.row),
                        xc_fixed=int(result.xc_fixed),
                        quality=int(result.quality),
                        valid=bool(result.valid),
                        flags=int(result.flags),
                    )
                )
            except AttributeError as exc:
                raise MockReceiverError("rows must contain RowResult or ProfilePoint objects") from exc
    profile = Profile(
        format_version=2,
        profile_id=profile_id,
        angle_raw=angle_raw,
        timestamp=timestamp,
        error_flags=error_flags,
        points=tuple(points),
        frame_width=frame_width,
        frame_height=frame_height,
    )
    return encode_profile(profile)


__all__ = [
    "MAX_SLP2_PACKET_BYTES", "MockReceiverError", "AxisWord", "CancellationRecord",
    "MockAxisReceiver", "encode_slp2_rows",
]
