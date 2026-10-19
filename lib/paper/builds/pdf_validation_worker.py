"""Portable/frozen worker entry: inspect one PDF and write a bounded report.

Launched only by pdf_validation.validate_pdf. Both desktop executables dispatch
this mode before application/tray startup. No application state is imported.
"""
from __future__ import annotations
from .contract import limit as limit

import json
import logging
import math
import os
import sys
from pathlib import Path

from .pdf_validation_limits import apply_limits, observed_memory_bytes, bind_parent_lifetime

# Stdlib logging only: this frozen worker must not import application state.
_log = logging.getLogger(__name__)

WORKER_FLAG = '--paper-validate-worker'


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) not in (3, 5):
        return 4
    source, output = Path(args[0]), Path(args[1])
    report = {'valid': False, 'pages': 0, 'objects': 0, 'decoded_bytes': 0,
              'observed_memory_bytes': 0, 'error': 'pdf_validation_unavailable'}
    try:
        memory = int(args[2])
        seconds = float(args[3]) if len(args) == 5 else 0
        if memory < 0 or not math.isfinite(seconds) or seconds < 0:
            return 4
        bind_parent_lifetime(int(args[4]) if len(args) == 5 else os.getppid())
        native_limits = apply_limits(memory, seconds)
        from .pdf_structure import inspect_pdf, PdfValidationFailure
        from pypdf.errors import LimitReachedError
        try:
            report.update(inspect_pdf(source), valid=True, error=None)
        except (MemoryError, LimitReachedError):
            report['error'] = 'pdf_limit'
        except PdfValidationFailure as exc:
            report['error'] = exc.code
        except Exception as exc:
            report['error'] = 'pdf_invalid'
            _log.debug('[PdfValidationWorker] inspect_pdf failed: %s', exc)
        report['observed_memory_bytes'] = observed_memory_bytes()
        if native_limits is not None and native_limits[1] is not None:
            native_limits[1].cancel()
    except MemoryError:
        report['error'] = 'pdf_limit'
    except (ImportError, OSError, ValueError):
        pass  # Missing parser/native limit support is not a successful check.
    output.write_text(json.dumps(report, separators=(',', ':')))
    return 0 if report['valid'] else {'pdf_invalid': 2, 'pdf_limit': 3}.get(report['error'], 4)


if __name__ == '__main__':
    raise SystemExit(main())
