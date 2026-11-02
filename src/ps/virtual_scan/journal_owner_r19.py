"""Keep the existing HDMI owner protocol; move only live logs to tmpfs.

Hardware-close confirmation and diagnostic-log persistence are separate facts.
Unconfirmed owners are never killed and their RAM logs are never removed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import traceback
import uuid


def _mountinfo():
    return Path('/proc/self/mountinfo').read_text()


def _mount_path(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def _require_tmpfs(root, read_mountinfo):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('owner RAM-log root must be a directory')
    matches = []
    for line in read_mountinfo().splitlines():
        before, separator, after = line.partition(' - ')
        if not separator:
            continue
        fields, filesystem = before.split(), after.split()
        if len(fields) < 5 or not filesystem:
            continue
        mount = Path(_mount_path(fields[4])).resolve()
        if root == mount or mount in root.parents:
            matches.append((len(mount.parts), filesystem[0]))
    if not matches or max(matches)[1] != 'tmpfs':
        raise ValueError('owner RAM logs require a verified tmpfs mount')
    return root


class JournalOwnerError(RuntimeError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.journal_receipt = receipt


class JournalHDMIOwner:
    """Delegate HDMI operations and exclusively persist final diagnostic logs.

    Test-only constructor injection does not instantiate a real device owner.
    Production ``factory`` always uses the explicit /dev/shm tmpfs root.
    """

    def __init__(self, config, *, remote_factory=None, tmpfs_root='/dev/shm',
                 read_mountinfo=_mountinfo):
        self.persistent_log = Path(config['owner_log']).resolve()
        self.persistent_journal = Path(str(self.persistent_log)+'.json')
        self.persistent_log.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.persistent_log, self.persistent_journal):
            if path.exists() or path.is_symlink():
                raise FileExistsError(f'owner evidence already exists: {path}')
        root = _require_tmpfs(tmpfs_root, read_mountinfo)
        self.ram_directory = Path(tempfile.mkdtemp(prefix='scan3d_r19_hdmi_', dir=root))
        self.ram_log = self.ram_directory/'owner.stdout.log'
        self.ram_journal = Path(str(self.ram_log)+'.json')
        self.owner_mocked = remote_factory is not None
        self.snapshots = []
        self._close_receipt = None
        self.owner = None
        if remote_factory is None:
            from structured_light.p2.hdmi_owner import RemoteHDMIOutput
            remote_factory = RemoteHDMIOutput
        # Same processed preview, ACK protocol, inherited lock and timeouts.
        os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
        try:
            self.owner = remote_factory(config['manifest'], owner_log=str(self.ram_log),
                                        timeout_s=5, startup_timeout_s=60, preview_only=True,
                                        hardware_lock_fd=config['hardware_lock_fd'], shared_canvas=True)
        except BaseException as exc:
            snapshot = self._persist('constructor_error', final=False)
            raise JournalOwnerError(self._failure_message('constructor', snapshot), snapshot) from exc

    @staticmethod
    def _copy_exclusive(source, destination):
        digest = hashlib.sha256()
        count = 0
        with source.open('rb') as incoming, destination.open('xb') as outgoing:
            for block in iter(lambda: incoming.read(1024*1024), b''):
                outgoing.write(block)
                digest.update(block)
                count += len(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        return dict(path=str(destination), bytes=count, sha256=digest.hexdigest(), fsynced=True)

    @staticmethod
    def _sync_directory(directory):
        if os.name != 'posix':
            return dict(path=str(directory), performed=False,
                        reason='directory fsync unavailable in non-POSIX offline tests')
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return dict(path=str(directory), performed=True, fsynced=True)

    def _persist(self, phase, *, final):
        target = self.persistent_log if final else self.persistent_log.with_name(
            self.persistent_log.name+'.'+phase+'.'+uuid.uuid4().hex)
        destinations = (target, Path(str(target)+'.json'))
        files, errors = {}, []
        for name, source, destination in zip(('stdout', 'journal'),
                (self.ram_log, self.ram_journal), destinations):
            try:
                files[name] = self._copy_exclusive(source, destination)
            except BaseException:
                errors.append(dict(kind=name, source=str(source), destination=str(destination),
                                   error=traceback.format_exc()))
        directory_sync = None
        try:
            directory_sync = self._sync_directory(target.parent)
        except BaseException:
            errors.append(dict(kind='directory_fsync', destination=str(target.parent),
                               error=traceback.format_exc()))
        receipt = dict(phase=phase, logs_persisted=not errors, files=files,
                       directory_fsync=directory_sync, persistence_errors=errors,
                       ram_log=str(self.ram_log),
                       ram_journal=str(self.ram_journal), ram_logs_retained=True,
                       owner_mocked=self.owner_mocked)
        self.snapshots.append(receipt)
        return receipt

    def _failure_message(self, operation, receipt):
        return (f'HDMI {operation} failed; RAM logs retained at {self.ram_directory}; '
                f'diagnostic snapshot persisted={receipt["logs_persisted"]}'
                +(f'; cause={receipt["original_error_type"]}: {receipt["original_error"]}'
                  if 'original_error_type' in receipt else ''))

    def _record_original_error(self, receipt, exc):
        """Preserve the first RPC error before later close/broken-pipe errors.

        This runs only on failure. It neither acknowledges a display frame nor
        changes the existing owner, timeout or DMA cleanup requirements.
        """
        receipt.update(original_error_type=type(exc).__name__,
                       original_error=str(exc), original_traceback=traceback.format_exc())
        context = {key: getattr(exc, key) for key in ('hdmi_rpc', 'hdmi_owner_diagnostics')
                   if isinstance(getattr(exc, key, None), dict)}
        receipt['original_error_context'] = context
        source=receipt.get('files',{}).get('stdout',{}).get('path')
        if source is None:
            return
        destination=Path(source+'.exception.json')
        try:
            import json
            with destination.open('x',encoding='utf8') as out:
                json.dump({key:receipt[key] for key in
                    ('original_error_type','original_error','original_traceback',
                     'original_error_context')},out,indent=2)
                out.flush();os.fsync(out.fileno())
            receipt['original_error_artifact']=str(destination)
        except BaseException:
            receipt['original_error_persistence_error']=traceback.format_exc()
            receipt['logs_persisted']=False

    def _proxy(self, operation, *args):
        if self._close_receipt is not None:
            raise RuntimeError('HDMI journal owner is closed')
        try:
            return getattr(self.owner, operation)(*args)
        except BaseException as exc:
            receipt = self._persist(operation+'_error', final=False)
            self._record_original_error(receipt, exc)
            raise JournalOwnerError(self._failure_message(operation, receipt), receipt) from exc

    def configure(self):
        return self._proxy('configure')

    def show_canvas(self, canvas):
        return self._proxy('show_canvas', canvas)

    def close(self):
        if self._close_receipt is not None:
            return self._close_receipt
        remote = None
        close_error = None
        try:
            remote = self.owner.close()
        except BaseException:
            close_error = traceback.format_exc()
        # Never infer successful shutdown from log content or persistence.
        confirmed = bool(isinstance(remote, dict) and remote.get('closed_confirmed') is True
                         and remote.get('owner_exited') is True and remote.get('returncode') == 0)
        persistence = self._persist('confirmed_close' if confirmed else 'unconfirmed_close', final=True)
        self._close_receipt = dict(
            closed_confirmed=confirmed,
            owner_exited=bool(isinstance(remote, dict) and remote.get('owner_exited') is True),
            returncode=remote.get('returncode') if isinstance(remote, dict) else None,
            owner_close_receipt=remote, owner_close_error=close_error,
            **persistence)
        return self._close_receipt


def factory(config):
    """Top-level ProcessDisplay factory; injected mocks are explicit test-only."""
    test_options = config.get('_journal_test_options')
    if test_options is not None:
        if not callable(test_options.get('remote_factory')):
            raise ValueError('journal test options require an explicit mock owner factory')
        return JournalHDMIOwner(config, **test_options)
    return JournalHDMIOwner(config)


ram_journal_owner = factory
