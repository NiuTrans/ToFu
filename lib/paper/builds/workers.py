"""Dispatch internal paper workers before frozen desktop application startup.

Both packaged entry points call this once before data, logging or tray setup.
Source launches use each worker's module entry directly.
"""
from __future__ import annotations

import sys


def dispatch_frozen_worker() -> None:
    if not getattr(sys, 'frozen', False):
        return
    from . import pdf_validation_worker, process_worker
    for worker in (pdf_validation_worker, process_worker):
        if sys.argv[1:2] == [worker.WORKER_FLAG]:
            raise SystemExit(worker.main(sys.argv[2:]))
