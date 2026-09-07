"""Storage-independent, compact JSONL journal for actionable log signals.

``error.log`` remains human-readable evidence.  This handler is the machine
index beside it: every admitted WARNING+ checkpoint becomes one bounded JSON
object with a stable fingerprint, true occurrence delta, correlation ids and
source location.  It deliberately has no database dependency, so the journal
still works when the storage sidecar is the incident being diagnosed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from lib.log_policy import LOG_FILE_MODE, stream_backup_count, stream_max_bytes
from lib.log_redaction import redact_text, sanitize_value


SCHEMA_VERSION = 1

_ID_TOKEN = r'[A-Za-z0-9._:-]{6,96}'
_CONVERSATION_PATTERNS = (
    re.compile(r'(?i)\b(?:conv(?:ersation)?(?:_id|Id)?)[=:]\s*["\']?(' + _ID_TOKEN + r')'),
    re.compile(r'\b(m[st][A-Za-z0-9]{8,32})\b'),
)
_TASK_PATTERNS = (
    re.compile(r'(?i)\btask(?:_id|Id)?[=:]\s*["\']?(' + _ID_TOKEN + r')'),
    re.compile(r'\[Task\s+(' + _ID_TOKEN + r')\]'),
    re.compile(r'\b(pt_[A-Za-z0-9]{8,64})\b'),
)
_TRACE_PATTERNS = (
    re.compile(r'(?i)\btrace(?:_id|Id)?[=:]\s*["\']?(' + _ID_TOKEN + r')'),
    re.compile(r'\b(chatcmpl-[A-Za-z0-9._:-]{6,96})\b'),
)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep each newly opened incident inode private across rollovers."""

    def _open(self):
        stream = super()._open()
        try:
            os.fchmod(stream.fileno(), LOG_FILE_MODE)
        except (AttributeError, OSError):
            try:
                os.chmod(self.baseFilename, LOG_FILE_MODE)
            except OSError:
                pass
        return stream


def _first_match(patterns, text: str) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)[:96]
    return ''


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _positive_int(value: object, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1, parsed)


def _safe_identifier(value: object, limit: int = 96) -> str:
    """Bound/redact identifier lanes that bypass the human log formatter."""
    ceiling = max(1, int(limit))
    return redact_text(value, max_chars=max(128, ceiling))[:ceiling]


def _incident_level(record: logging.LogRecord) -> str:
    if record.levelno >= logging.CRITICAL:
        return 'CRITICAL'
    if record.levelno >= logging.ERROR:
        return 'ERROR'
    return 'WARNING'


class IncidentJournalHandler(logging.Handler):
    """Write compact structured WARNING+ records through a rotating sink."""

    def __init__(self, path: str):
        super().__init__(logging.WARNING)
        self.path = os.path.abspath(path)
        self._sink = _PrivateRotatingFileHandler(
            self.path,
            maxBytes=stream_max_bytes('incident'),
            backupCount=stream_backup_count('incident'),
            encoding='utf-8',
            delay=True,
        )
        self._sink.setFormatter(logging.Formatter('%(message)s'))
        self._failure_lock = threading.Lock()
        self._failure_count = 0
        self._last_failure_notice = float('-inf')

    @staticmethod
    def _record_text(record: logging.LogRecord) -> str:
        try:
            return redact_text(record.getMessage(), max_chars=1_200)
        except Exception:
            return redact_text(record.msg, max_chars=1_200)

    @staticmethod
    def _fingerprint(record: logging.LogRecord, text: str) -> tuple[str, str]:
        fingerprint = str(getattr(record, 'tofu_fingerprint', '') or '')
        template = str(getattr(record, 'tofu_template', '') or '')
        if fingerprint and template:
            return fingerprint, template
        try:
            from lib.log_aggregates import fingerprint_text
            return fingerprint_text(record.levelname, record.name, text)
        except Exception:
            import hashlib
            raw = '%s|%s|%s' % (record.levelname, record.name, text[:512])
            return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:16], \
                text.split('\n', 1)[0][:200]

    @staticmethod
    def _exception_signature(record: logging.LogRecord, text: str) -> str:
        if record.exc_info:
            exc_type = getattr(record.exc_info[0], '__name__', '')
            tb = record.exc_info[2]
            while tb is not None and tb.tb_next is not None:
                tb = tb.tb_next
            if tb is not None:
                code = tb.tb_frame.f_code
                return '%s@%s:%s' % (
                    exc_type or '?', os.path.basename(code.co_filename), code.co_name)
            return exc_type
        try:
            from lib.log_aggregates import _exc_signature
            return _exc_signature(text)
        except Exception:
            return ''

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self._record_text(record)
            fingerprint, template = self._fingerprint(record, text)
            request_id = _safe_identifier(
                getattr(record, 'tofu_request_id', '')
                or getattr(record, 'request_id', '') or '')
            event_fields = sanitize_value(
                getattr(record, 'tofu_event_fields', {}) or {},
                field_name='event_fields', max_items=30, max_string_chars=600)
            event_fields = event_fields if isinstance(event_fields, dict) else {}

            def _field(*names: str) -> str:
                for name in names:
                    value = event_fields.get(name)
                    if value not in (None, ''):
                        return _safe_identifier(value)
                return ''

            entry = {
                'schema_version': SCHEMA_VERSION,
                'timestamp': _iso(record.created),
                'level': _incident_level(record),
                'logger': _safe_identifier(record.name, 256),
                'fingerprint': _safe_identifier(fingerprint, 64),
                'template': redact_text(template, max_chars=300),
                'occurrence_delta': _positive_int(getattr(
                    record, 'tofu_occurrence_delta', 1) or 1),
                'window_count': _positive_int(getattr(
                    record, 'tofu_window_count', 1) or 1),
                'event': _safe_identifier(
                    getattr(record, 'tofu_event_name', '') or '', 128),
                'request_id': request_id or _field('request_id', 'requestId'),
                'conversation_id': (
                    _field('conversation_id', 'conversationId', 'conv_id', 'convId')
                    or _first_match(_CONVERSATION_PATTERNS, text)),
                'task_id': (_field('task_id', 'taskId')
                            or _first_match(_TASK_PATTERNS, text)),
                'trace_id': (_field('trace_id', 'traceId')
                             or _first_match(_TRACE_PATTERNS, text)),
                'key_id': _safe_identifier(
                    getattr(record, 'tofu_key_id', '') or ''),
                'user_id': (_safe_identifier(
                    getattr(record, 'tofu_user_id', '') or '')
                            or _field('user_id', 'userId')),
                'exception': redact_text(
                    self._exception_signature(record, text), max_chars=256),
                'source': redact_text(
                    '%s:%s' % (os.path.basename(record.pathname), record.lineno),
                    max_chars=512),
                'sample': text,
            }
            if event_fields:
                entry['fields'] = event_fields
            # Empty values add noise to every line and convey no information.
            entry = {key: value for key, value in entry.items()
                     if value not in ('', None, {}, [])}
            line = json.dumps(entry, ensure_ascii=False, separators=(',', ':'),
                              default=str)
            journal_record = logging.LogRecord(
                name='incident', level=logging.INFO, pathname=__file__, lineno=0,
                msg=line, args=(), exc_info=None)
            self._sink.emit(journal_record)
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """Report sink failure without echoing a potentially secret record.

        ``logging.Handler.handleError`` prints the entire record when
        ``logging.raiseExceptions`` is enabled.  A disk-full incident must not
        turn that development-mode behavior into a credential leak or another
        warning loop, so this handler emits only one metadata-free heartbeat
        per minute.
        """
        now = time.monotonic()
        with self._failure_lock:
            self._failure_count += 1
            if now - self._last_failure_notice < 60.0:
                return
            failures = self._failure_count
            self._failure_count = 0
            self._last_failure_notice = now
        try:
            exception_type = getattr(sys.exc_info()[0], '__name__', 'Exception')
            sys.stderr.write(
                '[incident-journal] write failed; failures=%d type=%s\n'
                % (failures, exception_type))
            sys.stderr.flush()
        except Exception:
            pass

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        try:
            self._sink.close()
        finally:
            super().close()


__all__ = ['IncidentJournalHandler', 'SCHEMA_VERSION']
