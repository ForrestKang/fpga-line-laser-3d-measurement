"""Bounded MT9V034 context-A linear exposure preparation, independent of PYNQ.

MT9V034 Register Reference Rev A: p12 shutter, p16 gain, p23 AEC/AGC.
This prepares parameters only; it never resets the sensor or starts capture.
"""
import time


def manual_plan(snapshot, *, coarse_rows, gain_code=16):
    if type(coarse_rows) is not int or not 1 <= coarse_rows <= 32765:
        raise ValueError('coarse exposure must be 1..32765 rows')
    if type(gain_code) is not int or not 16 <= gain_code <= 64:
        raise ValueError('analog gain code must be 16..64 (1..4X)')
    for reg in (0x07, 0x0F, 0xAF, 0x35, 0x0B, 0xD5):
        if reg not in snapshot or type(snapshot[reg]) is not int or not 0 <= snapshot[reg] <= 65535:
            raise ValueError(f'missing or invalid sensor backup {reg:#x}')
    if snapshot[0x07] & 0x8000:
        raise ValueError('context B is unsupported; no registers written')
    if snapshot[0x0F] & 1:
        raise ValueError('HDR is unsupported; no registers written')
    if snapshot[0xD5] != 0 or snapshot[0x35] & 0x8000:
        raise ValueError('nonzero fine exposure / gain attenuation unsupported')
    return ((0xAF, snapshot[0xAF] & ~3),
            (0x35, (snapshot[0x35] & ~0x7F) | gain_code), (0x0B, coarse_rows))


def apply_manual(device, snapshot, *, coarse_rows, gain_code=16):
    plan = manual_plan(snapshot, coarse_rows=coarse_rows, gain_code=gain_code)
    for reg, value in plan:
        device.write_verified(reg, value)
    return plan


class SCCBAccess:
    def __init__(self, diagnostic, trace, save, *, clock=time.monotonic,
                 sleep=time.sleep, timeout_s=0.5):
        if not 0 < timeout_s <= 0.5:
            raise ValueError('SCCB host timeout must be in (0,0.5] seconds')
        self.diag, self.trace, self.save = diagnostic, trace, save
        self.clock, self.sleep, self.timeout_s = clock, sleep, timeout_s

    def _transaction(self, reg, value=None):
        if type(reg) is not int or not 0 <= reg <= 255:
            raise ValueError('8-bit sensor register required')
        if value is not None and (type(value) is not int or not 0 <= value <= 65535):
            raise ValueError('16-bit sensor value required')
        item = {'operation': 'read' if value is None else 'write',
                'register': reg, 'address_7bit': 0x48}
        if value is not None:
            item['written_value'] = value
        self.trace.append(item)
        self.save()  # preserve intent before every SCCB command
        try:
            if int(self.diag.read(0x38)) & 1:
                raise RuntimeError('SCCB busy before command; no command sent')
            self.diag.write(0x24, 0x48)
            self.diag.write(0x28, reg)
            if value is not None:
                self.diag.write(0x2C, value)
            self.diag.write(0x34, 1 if value is None else 2)
            deadline = self.clock() + self.timeout_s
            while True:
                status = int(self.diag.read(0x38))
                item['status'] = status
                if not status & 1 and status & 0xE:
                    if status != 2:
                        raise RuntimeError(f'SCCB {item["operation"]} failed: {status:#x}')
                    if value is None:
                        item['read_value'] = int(self.diag.read(0x30)) & 0xFFFF
                    self.save()
                    return item.get('read_value')
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError('SCCB host deadline expired; no retry')
                self.sleep(min(0.0005, remaining))
        except BaseException as exc:
            item['error'] = f'{type(exc).__name__}: {exc}'
            self.save()
            raise

    def read(self, reg):
        return self._transaction(reg)

    def write_verified(self, reg, value):
        self._transaction(reg, value)
        actual = self.read(reg)
        if actual != value:
            raise RuntimeError(f'SCCB readback mismatch at {reg:#x}: {actual:#x} != {value:#x}')
