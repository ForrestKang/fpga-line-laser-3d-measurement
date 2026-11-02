"""Opt-in R13 host trace; original pinned SCCB implementation stays unchanged.

No additional MMIO, retries, BUSY requirement, reset or error clearing.
Timestamps describe host observations, not exposure or byte-level ACK timing.
"""
from .camera_exposure import SCCBAccess


class TracedSCCBAccess(SCCBAccess):
    def _transaction(self, reg, value=None):
        if type(reg) is not int or not 0 <= reg <= 255:
            raise ValueError('8-bit sensor register required')
        if value is not None and (type(value) is not int or not 0 <= value <= 65535):
            raise ValueError('16-bit sensor value required')
        item = {'operation': 'read' if value is None else 'write',
                'register': reg, 'address_7bit': 0x48,
                'host_sequence': len(self.trace),
                'started_monotonic_s': self.clock(), 'poll_count': 0,
                'busy_observed': False, 'status_transitions': [],
                'status_transitions_truncated': False,
                'trace_scope': 'host MMIO observations; not sensor ACK phase or hardware transaction ID'}
        if value is not None:
            item['written_value'] = value
        self.trace.append(item)
        self.save()  # preserve intent before every SCCB command
        try:
            item['pre_command_status'] = int(self.diag.read(0x38))
            if item['pre_command_status'] & 1:
                raise RuntimeError('SCCB busy before command; no command sent')
            self.diag.write(0x24, 0x48)
            self.diag.write(0x28, reg)
            if value is not None:
                self.diag.write(0x2C, value)
            item['command_write_started_monotonic_s'] = self.clock()
            self.diag.write(0x34, 1 if value is None else 2)
            item['command_write_returned_monotonic_s'] = self.clock()
            deadline = item['command_write_returned_monotonic_s'] + self.timeout_s
            while True:
                status = int(self.diag.read(0x38))
                observed_at = self.clock()
                item['poll_count'] += 1
                item['busy_observed'] |= bool(status & 1)
                if item.get('status') != status:
                    if len(item['status_transitions']) < 64:
                        item['status_transitions'].append(
                            {'status': status, 'monotonic_s': observed_at})
                    else:
                        item['status_transitions_truncated'] = True
                item['status'] = status
                if not status & 1 and status & 0xE:
                    item['terminal_monotonic_s'] = observed_at
                    item['terminal_flags'] = dict(done=bool(status & 2),
                        ack_error_or_rejected_command=bool(status & 4), timeout_error=bool(status & 8))
                    if status != 2:
                        raise RuntimeError(f'SCCB {item["operation"]} failed: {status:#x}')
                    if value is None:
                        item['read_value'] = int(self.diag.read(0x30)) & 0xFFFF
                    self.save()
                    return item.get('read_value')
                remaining = deadline - observed_at
                if remaining <= 0:
                    raise TimeoutError('SCCB host deadline expired; no retry')
                self.sleep(min(0.0005, remaining))
        except BaseException as exc:
            item['error'] = f'{type(exc).__name__}: {exc}'
            item['error_monotonic_s'] = self.clock()
            self.save()
            raise

