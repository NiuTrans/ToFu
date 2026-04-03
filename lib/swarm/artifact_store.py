# HOT_PATH — functions in this module are called per-request.
# Do NOT use logger.info() here; use logger.debug() instead.
"""lib/swarm/artifact_store.py — Pluggable artifact storage for inter-agent data sharing.

Extracted from protocol.py for modularity.

Contains:
  • ArtifactBackend  — ABC for pluggable storage backends
  • InMemoryBackend  — default in-memory backend (fast, no persistence)
  • ArtifactStore    — thread-safe shared storage with pluggable backends
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  ArtifactBackend — Abstract interface for pluggable backends
# ═══════════════════════════════════════════════════════════

class ArtifactBackend(ABC):
    """Abstract interface for artifact storage backends.

    Implementations must be thread-safe if used from multiple threads.
    The ``ArtifactStore`` wraps access with its own lock, so single-threaded
    backends are acceptable.

    Each entry is a dict with at least: ``content``, ``writer``, ``timestamp``,
    ``size``, ``tags``.
    """

    @abstractmethod
    def put(self, key: str, entry: dict) -> dict | None:
        """Store entry dict, return old entry if overwritten, else None."""
        ...

    @abstractmethod
    def get(self, key: str) -> dict | None:
        """Return entry dict or None."""
        ...

    @abstractmethod
    def delete(self, key: str) -> dict | None:
        """Delete and return entry, or None if not found."""
        ...

    @abstractmethod
    def keys(self) -> list[str]:
        """All stored keys (no ordering guarantees)."""
        ...

    @abstractmethod
    def items(self) -> list[tuple[str, dict]]:
        """All (key, entry) pairs."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Remove all entries."""
        ...

    @abstractmethod
    def __len__(self) -> int:
        ...


# ─── InMemoryBackend ──────────────────────────────────────

class InMemoryBackend(ArtifactBackend):
    """Default: fast, in-memory, no persistence.  Lost on process exit."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def put(self, key: str, entry: dict) -> dict | None:
        old = self._store.get(key)
        self._store[key] = entry
        return old

    def get(self, key: str) -> dict | None:
        return self._store.get(key)

    def delete(self, key: str) -> dict | None:
        return self._store.pop(key, None)

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def items(self) -> list[tuple[str, dict]]:
        return list(self._store.items())

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ═══════════════════════════════════════════════════════════
#  ArtifactStore — thread-safe shared storage
# ═══════════════════════════════════════════════════════════

class ArtifactStore:
    """Thread-safe shared storage for inter-agent data sharing.

    Features:
      • Pluggable backend — InMemoryBackend (default), or implement ArtifactBackend
      • TTL-based expiration — artifacts expire after ``max_age_seconds``
      • Size limits — total chars capped at ``max_total_chars``
      • Tags — artifacts can be tagged for filtered listing
      • Lazy cleanup — expired entries are evicted on access

    Usage::

        store = ArtifactStore()
        store.put('analysis_results', 'The data shows...', writer_id='agent-1',
                  tags=['analysis', 'phase1'])
        content = store.get('analysis_results')
        keys = store.list_keys(tag='analysis')
    """

    def __init__(
        self,
        max_total_chars: int = 10_000_000,
        max_age_seconds: float = 3600,
        backend: ArtifactBackend | None = None,
        # Legacy compat: accept max_items kwarg (ignored)
        max_items: int = 500,
    ) -> None:
        self._backend = backend or InMemoryBackend()
        self._lock = threading.Lock()
        self._total_chars = 0
        self._max_total_chars = max_total_chars
        self._max_age = max_age_seconds

    # ── Write ──

    def put(self, key: str, content: str, writer_id: str = '',
            tags: list | None = None,
            # Legacy compat: accept agent_id as alias for writer_id
            agent_id: str = '') -> None:
        """Write or overwrite an artifact."""
        _writer = writer_id or agent_id
        with self._lock:
            self._evict_expired_locked()
            old = self._backend.put(key, {
                'content': content,
                'writer': _writer,
                'agent_id': _writer,   # backward compat field
                'timestamp': time.time(),
                'size': len(content),
                'tags': tags or [],
            })
            if old:
                self._total_chars -= old.get('size', 0)
                logger.debug('[ArtifactStore] OVERWRITE key=%s writer=%s old_size=%d new_size=%d tags=%s',
                            key, _writer, old.get('size', 0), len(content), tags or [])
            else:
                logger.debug('[ArtifactStore] PUT key=%s writer=%s size=%d tags=%s total_chars=%d',
                            key, _writer, len(content), tags or [], self._total_chars + len(content))
            self._total_chars += len(content)
            # Evict oldest if over size limit
            while self._total_chars > self._max_total_chars and len(self._backend) > 0:
                all_items = self._backend.items()
                if not all_items:
                    break
                oldest_key = min(all_items, key=lambda kv: kv[1].get('timestamp', 0))[0]
                if oldest_key == key:
                    break  # don't evict the entry we just wrote
                removed = self._backend.delete(oldest_key)
                if removed:
                    self._total_chars -= removed.get('size', 0)
                    logger.debug('[ArtifactStore] EVICT key=%s size=%d (over limit %d)',
                                oldest_key, removed.get('size', 0), self._max_total_chars)

    # ── Read ──

    def get(self, key: str) -> str | None:
        """Read an artifact.  Returns ``None`` if not found or expired."""
        with self._lock:
            entry = self._backend.get(key)
            if not entry:
                logger.debug('[ArtifactStore] GET key=%s → NOT FOUND', key)
                return None
            age = time.time() - entry.get('timestamp', 0)
            if self._max_age and age > self._max_age:
                self._backend.delete(key)
                self._total_chars -= entry.get('size', 0)
                logger.debug('[ArtifactStore] GET key=%s → EXPIRED (age=%.0fs > max=%ds)',
                             key, age, self._max_age)
                return None
            logger.debug('[ArtifactStore] GET key=%s → OK size=%d age=%.0fs',
                        key, entry.get('size', 0), age)
            return entry.get('content')

    def has(self, key: str) -> bool:
        """Check if an artifact exists (and is not expired)."""
        return self.get(key) is not None

    # ── List / Query ──

    def list_keys(self, tag: str | None = None) -> list:
        """List artifact keys, optionally filtered by tag.

        Returns list of key strings (if no tag) or list of metadata dicts
        for backward compatibility.  When called with ``tag=None`` or
        ``tag=''``, returns plain key strings.
        """
        with self._lock:
            self._evict_expired_locked()
            if not tag:
                return self._backend.keys()
            return [k for k, v in self._backend.items()
                    if tag in v.get('tags', [])]

    def get_all(self) -> dict[str, str]:
        """Get all non-expired artifacts as ``{key: content}``."""
        with self._lock:
            self._evict_expired_locked()
            return {k: v.get('content', '') for k, v in self._backend.items()}

    def summary(self, max_preview: int = 120) -> str:
        """Human-readable summary of all artifacts."""
        with self._lock:
            self._evict_expired_locked()
            items = self._backend.items()
            if not items:
                return '(no shared artifacts)'
            lines = []
            for k, v in items:
                content = v.get('content', '')
                preview = content[:max_preview].replace('\n', ' ')
                if len(content) > max_preview:
                    preview += '…'
                tag_str = f' [{", ".join(v.get("tags", []))}]' if v.get('tags') else ''
                writer = v.get('writer', v.get('agent_id', ''))
                size = v.get('size', len(content))
                lines.append(
                    f'  • [{k}]{tag_str} ({size:,} chars, by {writer}): {preview}'
                )
            return '\n'.join(lines)

    # ── Cleanup ──

    def clear(self) -> None:
        """Remove all artifacts."""
        with self._lock:
            self._backend.clear()
            self._total_chars = 0

    def cleanup(self) -> None:
        """Manually evict expired entries."""
        with self._lock:
            self._evict_expired_locked()

    def _evict_expired_locked(self) -> None:
        """Remove entries older than max_age.  Must hold ``self._lock``."""
        if not self._max_age:
            return
        now = time.time()
        expired = [k for k, v in self._backend.items()
                   if (now - v.get('timestamp', 0)) > self._max_age]
        if expired:
            logger.debug('[ArtifactStore] _evict_expired: removing %d expired key(s): %s',
                        len(expired), expired)
        for k in expired:
            removed = self._backend.delete(k)
            if removed:
                self._total_chars -= removed.get('size', 0)
                logger.debug('[ArtifactStore] evicted key=%s age=%.0fs size=%d',
                             k, now - removed.get('timestamp', 0), removed.get('size', 0))

    # ── Properties ──

    @property
    def total_chars(self) -> int:
        """Total chars stored across all artifacts."""
        with self._lock:
            return self._total_chars

    @property
    def size(self) -> int:
        """Number of artifacts (alias for len())."""
        with self._lock:
            return len(self._backend)

    def __len__(self) -> int:
        with self._lock:
            return len(self._backend)

    def __repr__(self) -> str:
        backend_name = type(self._backend).__name__
        return f'ArtifactStore({len(self)} artifacts, {self.total_chars:,} chars, {backend_name})'
