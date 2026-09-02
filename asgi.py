"""ASGI application entry for ``hypercorn asgi:app`` / ``uvicorn asgi:app``."""

import logging
import os
import sys

from server import (
    _acquire_instance_lock, _tofu_data_root, create_production_app)

# server.py takes the single-instance lock only on the ``python server.py``
# path (``__name__ == '__main__'``).  This module is the uvicorn/hypercorn
# entry, which previously bypassed the lock entirely: a second instance from
# the same project directory was rejected only deep inside startup, by the
# storage-sidecar lease.  Acquire the same lock here so every entry point
# fails fast with a clear message.  Keep the fd referenced for the whole
# process lifetime — closing it releases the flock.
_lock_dir = _tofu_data_root()
os.makedirs(_lock_dir, exist_ok=True)
_lock_ok, _instance_lock_fd = _acquire_instance_lock(
    os.path.join(_lock_dir, '.server.lock'),
    logging.getLogger('server'), mark_booting=True)
if not _lock_ok:
    if (os.environ.get('TOFU_SKIP_LOCK', '') or '').strip() != '1':
        sys.stderr.write(
            '[asgi.py] ERROR: Another server instance is already running from '
            'this project directory. Set TOFU_SKIP_LOCK=1 to force start.\n')
        sys.stderr.flush()
        raise SystemExit(1)
    logging.getLogger('server').warning(
        '[Lock] TOFU_SKIP_LOCK=1 — bypassing instance lock')
    _instance_lock_fd = None

app = create_production_app()


__all__ = ['app']
