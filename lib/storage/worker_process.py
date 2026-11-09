"""Process-bound worker identity codec and local death evidence.

The lifecycle contract owns the existing claimOwner encoding. Kernel evidence
only prevents timeout takeover; the job's epoch/token still own every write.
No process lookup is cached across authority requests.
"""
import errno
import os
import re
import select
import socket

from lib.process_owner import process_owner, _process_stat
from lib.storage.generated_worker_job_contract import WORKER_PROCESS_OWNER_PREFIX

_IDENTITY = re.compile(
    re.escape(WORKER_PROCESS_OWNER_PREFIX)
    + r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    + r':([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*):([0-9a-f]{32})\Z')


def parse_worker_process(worker_id):
    match = _IDENTITY.fullmatch(worker_id) if isinstance(worker_id, str) else None
    if match is None or len(worker_id) > 256:
        return None
    boot, namespace, pid, started, _ = match.groups()
    if int(pid) > 2**31 - 1 or max(int(namespace), int(started)) > 2**64 - 1:
        return None
    return dict(host=socket.gethostname(), boot=boot, pid_namespace=namespace,
                pid=int(pid), started=started)


def worker_process_id(instance, *, legacy_prefix):
    process = process_owner()
    if process is not None:
        identity = WORKER_PROCESS_OWNER_PREFIX + ':'.join(
            str(process[field]) for field in ('boot', 'pid_namespace', 'pid', 'started')) + ':' + instance
        if parse_worker_process(identity) is not None:
            return identity
    return legacy_prefix + ':' + instance


def process_claim_protected(worker_id):
    """No elapsed deadline can replace a captured, not-proved-dead process.

    pidfd/kill prove existence independently of procfs hidepid. Missing procfs
    files alone cannot prove exit. Namespace mismatch conservatively retains
    ownership; only the current storage epoch can invalidate that evidence.
    """
    process = parse_worker_process(worker_id)
    if process is None:
        return False
    current = process_owner()
    if current is None or any(process[key] != current[key] for key in ('boot', 'pid_namespace')):
        return True
    try:
        from pathlib import Path
        pids = next(line.split()[1:] for line in Path('/proc/self/status').read_text().splitlines()
                    if line.startswith('NSpid:'))
        if pids != [str(os.getpid())]:
            return True
        try:
            fd = os.pidfd_open(process['pid'])
        except (AttributeError, OSError) as error:
            if isinstance(error, OSError) and error.errno != errno.ENOSYS:
                raise
            os.kill(process['pid'], 0)
        else:
            try:
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                if any(events & select.POLLIN for _, events in poll.poll(0)):
                    return False
            finally:
                os.close(fd)
        # A differing start time proves PID reuse. A missing stat can be hidden
        # by procfs permissions, so ask the kernel again before trusting death.
        started, state, threads = _process_stat(process['pid'])
        return not (started != process['started'] or (state in {'Z', 'X', 'x'} and threads == 1))
    except ProcessLookupError:
        return False
    except (OSError, ValueError, IndexError, StopIteration):
        return True
