"""File-backed completed translations for one task's preview lifetime.

One lazy anonymous file stores source/translation pairs; views retain only keys
and byte ranges. Pair publication follows complete writes, so failed replacement
preserves the prior paid result. Empty views release the file. Callers serialize
snapshot/commit/cancel through their task cache lock; no durable authority lives
here and no worker or admission quota is introduced.
"""
from collections.abc import Mapping, MutableMapping
from contextlib import nullcontext
import os
import tempfile


class CompletedTranslationCache:
    def __init__(self):
        self._file = None
        self._ranges = {}
        self._counts = {'originals': 0, 'translations': 0}
        self._live_bytes = 0
        self._stored_bytes = 0

    def view(self, field):
        if field not in self._counts:
            raise ValueError('unknown translation cache field')
        return _TextView(self, field)

    def _append(self, raw):
        if self._file is None:
            self._file = tempfile.TemporaryFile(mode='w+b', buffering=0)
        offset = self._file.seek(0, 2)
        try:
            remaining = memoryview(raw)
            while remaining:
                written = self._file.write(remaining)
                if not written:
                    raise OSError('translation cache write made no progress')
                remaining = remaining[written:]
        finally:
            self._stored_bytes = self._file.seek(0, 2)
        return offset

    def _publish(self, field, key, offset, length):
        identity = (field, key)
        previous = self._ranges.get(identity)
        if previous is None:
            self._counts[field] += 1
        self._live_bytes += length - (previous[1] if previous else 0)
        self._ranges[identity] = (offset, length)

    def put_pair(self, key, original, translation):
        source, result = original.encode('utf-8'), translation.encode('utf-8')
        offset = self._append(source + result)
        self._publish('originals', key, offset, len(source))
        self._publish('translations', key, offset + len(source), len(result))
        self._try_reclaim()

    def _set(self, field, key, text):
        raw = text.encode('utf-8')
        offset = self._append(raw)
        self._publish(field, key, offset, len(raw))
        self._try_reclaim()

    def _read(self, field, key):
        offset, length = self._ranges[(field, key)]
        self._file.seek(offset)
        raw = self._file.read(length)
        if len(raw) != length:
            raise OSError('translation cache ended before its owned range')
        return raw.decode('utf-8')

    def _delete(self, field, key):
        _, length = self._ranges.pop((field, key))
        self._counts[field] -= 1
        self._live_bytes -= length
        self._try_reclaim()

    def _try_reclaim(self):
        try:
            self._reclaim()
        except OSError:
            # Housekeeping cannot revoke accepted ranges. A failed copy keeps
            # the original file; the next mutation may retry reclamation.
            from lib.log import get_logger
            get_logger(__name__).warning('[IncTranslate] completed cache reclamation deferred')

    def _reclaim(self):
        if self._file is None:
            return
        if not self._ranges:
            self._file.close()
            self._file = None
            self._stored_bytes = 0
            return
        if self._stored_bytes <= self._live_bytes * 2:
            return
        replacement = tempfile.TemporaryFile(mode='w+b', buffering=0)
        ranges = {}
        try:
            for identity, (offset, length) in self._ranges.items():
                start = replacement.tell()
                self._file.seek(offset)
                remaining = length
                while remaining:
                    raw = self._file.read(min(remaining, 64 * 1024))
                    if not raw:
                        raise OSError('translation cache reclamation found an incomplete range')
                    view = memoryview(raw)
                    while view:
                        written = replacement.write(view)
                        if not written:
                            raise OSError('translation cache reclamation made no progress')
                        view = view[written:]
                    remaining -= len(raw)
                ranges[identity] = (start, length)
        except BaseException:
            replacement.close()
            raise
        previous = self._file
        self._file, self._ranges = replacement, ranges
        self._stored_bytes = self._live_bytes
        previous.close()

    def freeze(self, *, read_lock=None):
        """Pin immutable ranges and their file until a borrowed commit returns."""
        ranges = dict(self._ranges)
        file = None
        if ranges:
            descriptor = os.dup(self._file.fileno())
            try:
                file = os.fdopen(descriptor, 'rb', buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
        return _FrozenCache(file, ranges, dict(self._counts), read_lock)

    def clear(self):
        self._ranges.clear()
        self._counts = {'originals': 0, 'translations': 0}
        self._live_bytes = 0
        self._reclaim()

    def snapshot(self):
        return {'originals': self._counts['originals'], 'translations': self._counts['translations'],
                'liveBytes': self._live_bytes, 'fileBytes': self._stored_bytes,
                'fileOpen': self._file is not None}


class _TextView(MutableMapping):
    def __init__(self, cache, field):
        self._cache, self._field = cache, field

    def __getitem__(self, key):
        return self._cache._read(self._field, key)

    def __setitem__(self, key, value):
        self._cache._set(self._field, key, value)

    def __delitem__(self, key):
        self._cache._delete(self._field, key)

    def __iter__(self):
        return (key for field, key in self._cache._ranges if field == self._field)

    def __contains__(self, key):
        return (self._field, key) in self._cache._ranges

    def __len__(self):
        return self._cache._counts[self._field]

    def clear(self):
        cache = self._cache
        for identity in tuple(cache._ranges):
            if identity[0] == self._field:
                _, length = cache._ranges.pop(identity)
                cache._live_bytes -= length
        cache._counts[self._field] = 0
        cache._try_reclaim()


class _FrozenCache:
    """Immutable metadata plus one pinned file; contains no decoded body cache."""
    def __init__(self, file, ranges, counts, read_lock):
        self._file, self._ranges, self._counts = file, ranges, counts
        self._read_lock = read_lock
        self.originals = _FrozenTextView(self, 'originals')
        self.translations = _FrozenTextView(self, 'translations')

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        self._ranges.clear()
        self._counts = {'originals': 0, 'translations': 0}

    def _read(self, field, key):
        offset, length = self._ranges[(field, key)]
        pread = getattr(os, 'pread', None)
        if pread is not None:
            pieces = []
            remaining = length
            while remaining:
                raw = pread(self._file.fileno(), remaining, offset)
                if not raw:
                    raise OSError('translation snapshot ended before its pinned range')
                pieces.append(raw)
                offset += len(raw)
                remaining -= len(raw)
            return b''.join(pieces).decode('utf-8')
        # Duplicate descriptors share a seek cursor. The task cache lock keeps
        # fallback reads independent of concurrent append/reclamation seeks.
        with self._read_lock if self._read_lock is not None else nullcontext():
            self._file.seek(offset)
            raw = self._file.read(length)
        if len(raw) != length:
            raise OSError('translation snapshot ended before its pinned range')
        return raw.decode('utf-8')


class _FrozenTextView(Mapping):
    def __init__(self, snapshot, field):
        self._snapshot, self._field = snapshot, field

    def __getitem__(self, key):
        return self._snapshot._read(self._field, key)

    def __contains__(self, key):
        return (self._field, key) in self._snapshot._ranges

    def __iter__(self):
        return (key for field, key in self._snapshot._ranges if field == self._field)

    def __len__(self):
        return self._snapshot._counts[self._field]
