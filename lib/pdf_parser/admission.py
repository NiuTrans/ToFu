"""Process-wide unfinished-work admission for classic PDF extraction."""

from __future__ import annotations

import threading


class PdfParseCapacityExceeded(RuntimeError):
    """The finite classic-PDF payload allowance is already occupied."""


class PdfParseTimeoutError(TimeoutError):
    """The caller wait expired; bounded worker work may still be settling."""


class _AdmissionLease:
    """Release one unfinished-work count exactly once across threads."""

    def __init__(self, owner: '_ParseAdmission') -> None:
        self._owner = owner
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._owner.release()


class _ParseAdmission:
    """Non-blocking aggregate admission for compressed PDF payloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._unfinished = 0
        self._peak_unfinished = 0
        self._rejected = 0

    def reserve(self, capacity: int) -> _AdmissionLease:
        with self._lock:
            if self._unfinished >= max(1, int(capacity)):
                self._rejected += 1
                raise PdfParseCapacityExceeded(
                    'Classic PDF parser is at capacity; retry shortly')
            self._unfinished += 1
            self._peak_unfinished = max(
                self._peak_unfinished, self._unfinished)
        return _AdmissionLease(self)

    def release(self) -> None:
        with self._lock:
            self._unfinished = max(0, self._unfinished - 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                'unfinished': self._unfinished,
                'peak_unfinished': self._peak_unfinished,
                'rejected': self._rejected,
            }


CLASSIC_PDF_ADMISSION = _ParseAdmission()


__all__ = [
    'CLASSIC_PDF_ADMISSION',
    'PdfParseCapacityExceeded',
    'PdfParseTimeoutError',
]
