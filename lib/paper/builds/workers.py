"""Dispatch internal command and validation workers before frozen app startup.

Both packaged entry points call this once before data, logging or tray setup.
Source launches use each worker's module entry directly.
"""
from __future__ import annotations

import sys


def dispatch_frozen_worker() -> None:
    if not getattr(sys, 'frozen', False):
        return
    if sys.argv[1:2] == ['--artifact-image-worker']:
        from lib.artifacts.image_validation_worker import main
        raise SystemExit(main(sys.argv[2:]))
    from . import pdf_validation_worker, process_worker
    for worker in (pdf_validation_worker, process_worker):
        if sys.argv[1:2] == [worker.WORKER_FLAG]:
            raise SystemExit(worker.main(sys.argv[2:]))
