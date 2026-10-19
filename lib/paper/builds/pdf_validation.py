"""Supervise one finite PDF validation process inside an admitted build.

Source and frozen Desktop launches use the same worker entry. The child reads
the staged PDF directly; the only IPC artifact is a bounded temporary report.
Cancellation, timeout and memory pressure kill and reap it before returning.
"""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .budget import validation_memory_bytes
from .contract import limit
from .errors import BuildError, check_cancelled
from .pdf_validation_worker import WORKER_FLAG

_MESSAGES = {
    'pdf_invalid': 'Build produced a malformed or unreadable PDF',
    'pdf_limit': 'PDF validation could not complete within available resources or explicit execution limits',
    'pdf_validation_unavailable': 'The PDF validation worker is unavailable',
}


def _command(path: Path, report: Path, memory: int, seconds: float = 0) -> list[str]:
    mode = [WORKER_FLAG] if getattr(sys, 'frozen', False) else ['-m', 'lib.paper.builds.pdf_validation_worker']
    return [sys.executable, *mode, str(path), str(report), str(memory), str(seconds), str(os.getpid())]


def validate_pdf(path: Path, *, cancel=None, timeout: float | None = None) -> dict:
    check_cancelled(cancel)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise BuildError('pdf_invalid', _MESSAGES['pdf_invalid'])
        with path.open('rb') as source:
            header = source.read(8)
            source.seek(max(0, info.st_size - 2048))
            if not header.startswith(b'%PDF-') or b'%%EOF' not in source.read(2048):
                raise BuildError('pdf_invalid', _MESSAGES['pdf_invalid'])
    except OSError as exc:
        raise BuildError('pdf_invalid', _MESSAGES['pdf_invalid']) from exc
    import psutil
    memory = validation_memory_bytes()
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise BuildError('pdf_limit', _MESSAGES['pdf_limit'])
    seconds = 0 if timeout is None else timeout
    deadline = None if timeout is None else time.monotonic() + seconds
    with tempfile.TemporaryDirectory(prefix='.pdf-validation-', dir=path.parent) as temporary:
        report_path = Path(temporary) / 'result.json'
        try:
            process = subprocess.Popen(_command(path.resolve(), report_path.resolve(), memory, seconds),
                cwd=Path(__file__).resolve().parents[3], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BuildError('pdf_validation_unavailable', _MESSAGES['pdf_validation_unavailable']) from exc
        try:
            observed_memory = 0
            try:
                child = psutil.Process(process.pid)
            except psutil.NoSuchProcess:
                child = None
            while process.poll() is None:
                check_cancelled(cancel)
                if deadline is not None and time.monotonic() >= deadline:
                    raise BuildError('pdf_limit', _MESSAGES['pdf_limit'])
                try:
                    if child is not None:
                        observed_memory = max(observed_memory, child.memory_info().rss)
                    if memory and observed_memory > memory:
                        raise BuildError('pdf_limit', _MESSAGES['pdf_limit'])
                except psutil.NoSuchProcess:
                    pass  # wait/poll, rather than observation, owns process completion
                except psutil.Error as exc:
                    raise BuildError('pdf_validation_unavailable', _MESSAGES['pdf_validation_unavailable']) from exc
                try:
                    process.wait(timeout=0.02)
                except subprocess.TimeoutExpired:
                    continue
            check_cancelled(cancel)
            if process.returncode == 3 or process.returncode < 0:
                raise BuildError('pdf_limit', _MESSAGES['pdf_limit'])
            if not report_path.is_file() or report_path.stat().st_size > limit('pdf_validation_report_bytes'):
                raise BuildError('pdf_validation_unavailable', _MESSAGES['pdf_validation_unavailable'])
            try:
                result = json.loads(report_path.read_bytes())
                metrics = ('pages', 'objects', 'decoded_bytes', 'observed_memory_bytes')
                if not isinstance(result, dict) or set(result) != {'valid', 'error', *metrics}:
                    raise ValueError('invalid report fields')
                if type(result['valid']) is not bool or any(
                    type(result[name]) is not int or result[name] < 0
                    for name in metrics):
                    raise ValueError('invalid report bounds')
                if memory and result['observed_memory_bytes'] > memory:
                    raise ValueError('invalid memory observation')
                if not result['valid']:
                    code = result['error'] if result['error'] in _MESSAGES else 'pdf_validation_unavailable'
                    raise BuildError(code, _MESSAGES[code])
                if process.returncode != 0 or result['error'] is not None or result['pages'] < 1:
                    raise ValueError('invalid validation result')
                result['observed_memory_bytes'] = max(result['observed_memory_bytes'], observed_memory)
                return result
            except (ValueError, TypeError) as exc:
                raise BuildError('pdf_validation_unavailable', _MESSAGES['pdf_validation_unavailable']) from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
