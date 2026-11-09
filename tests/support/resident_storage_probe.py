"""Disposable resident storage host whose stop outcome a test selects.

Used only by the manager restart specs in
``tests/server/test_restart_always_serves.py``. The manager launches it with
the resident protocol of ``serverctl_pkg.native_storage_host`` (published
record, inherited lease and manager-control pipe). On SIGTERM it reads
``probe-stop-mode`` in its working directory:

- ``clean`` (default): publishes ``stopped``.
- ``stale``: exits without a terminal record, as the 2026-11-06 22:40 host did
  when its daemon wait raised past the finalizer.
- ``timeout``: publishes ``failed`` with a ``TimeoutError``, a drain whose
  bounded stop expired.
- ``hang``: ignores SIGTERM, a host still inside its drain past the manager's
  stop budget.

Manager EOF always ends the probe, so a dead test leaves no host behind.
"""
import argparse
from pathlib import Path
import select
import signal
import threading

from serverctl_pkg.native_storage_service import RECORD_FORMAT
from serverctl_pkg.resident_service import (
    await_published_record, manager_control_matches, write_record,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--service-id')
    parser.add_argument('--state-file')
    for name in ('--manager-pid', '--manager-start', '--manager-fd', '--lease-fd'):
        parser.add_argument(name, type=int)
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    state = Path(args.state_file)
    record = await_published_record(
        state, service_id=args.service_id, manager_pid=args.manager_pid,
        manager_start=args.manager_start, record_format=RECORD_FORMAT, stopped=stopped)
    if not record or not manager_control_matches(record, args.manager_fd, write=False):
        return 1

    def owner_gone() -> bool:
        return bool(select.select([args.manager_fd], [], [], 0)[0])

    record['state'] = 'ready'
    write_record(state, record)
    while not stopped.wait(0.05):
        if owner_gone():
            return 0
    mode_file = Path('probe-stop-mode')
    mode = mode_file.read_text().strip() if mode_file.exists() else 'clean'
    if mode == 'hang':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while not owner_gone():
            threading.Event().wait(0.05)
        return 0
    if mode == 'stale':
        return 0
    record['state'] = 'failed' if mode == 'timeout' else 'stopped'
    record['error'] = ('TimeoutError: native daemon drain did not finish within its budget'
                       if mode == 'timeout' else '')
    write_record(state, record)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
