"""Temporary benchmark records without cohort-sized Python retention.

Append scalar evidence during a memory observation. Read it only after the
worker has frozen its residency counters. The context owns and closes the
unlinked file; no journal is a product cache or durable user state.
"""
import hashlib
import json
import tempfile
from threading import Lock


class RecordJournal:
    def __init__(self):
        self._lock = Lock()
        self._file = tempfile.TemporaryFile(mode='w+b', buffering=0)
        self._digest = hashlib.sha256()
        self.count = 0
        self.bytes_written = 0

    def __enter__(self):
        return self

    def __exit__(self, _kind, primary, _traceback):
        try:
            self._file.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note('benchmark journal cleanup failed: ' + type(cleanup).__name__)

    def append(self, record):
        encoded = json.dumps(record, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        with self._lock:
            remaining = memoryview(encoded)
            while remaining:
                written = self._file.write(remaining)
                if written is None or written <= 0:
                    raise OSError('benchmark record journal made no write progress')
                remaining = remaining[written:]
            self._digest.update(encoded)
            self.count += 1
            self.bytes_written += len(encoded)

    def read(self):
        with self._lock:
            self._file.seek(0)
            digest = hashlib.sha256()
            rows = []
            size = 0
            for line in self._file:
                digest.update(line)
                size += len(line)
                rows.append(json.loads(line))
            self._file.seek(0, 2)
            if len(rows) != self.count or size != self.bytes_written or digest.digest() != self._digest.digest():
                raise ValueError('benchmark record journal changed or is incomplete')
            return rows

    def receipt(self):
        return dict(records=self.count, bytes=self.bytes_written,
                    sha256=self._digest.hexdigest(), storage='unlinked_temporary_file')
