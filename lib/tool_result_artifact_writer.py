"""Bounded incremental spool for large tool results.

``ToolResultArtifactWriter`` accepts one tool result as a stream of text
chunks, keeps it fully in memory while it fits the same presentation scale as
``lib.project_mod.config.MAX_COMMAND_OUTPUT``, and — once the stream exceeds
that scale — atomically moves it to a restrictive-permission temporary spool
OUTSIDE any user project. Finalization persists the full content through the
owner-scoped :class:`lib.tool_result_artifacts.ToolResultArtifactRepository`
and returns a bounded head/tail preview plus the opaque ``artifactRef``; a
quota or disk failure instead degrades to that bounded preview with no durable
reference (unretained overflow).

Raw filesystem paths never leave this module: callers receive only the opaque
repository reference and the preview text.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from lib.log import get_logger
from lib.project_mod.config import MAX_COMMAND_OUTPUT
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

_MIB = 1024 * 1024
#: Hard ceiling for one persisted tool-result artifact, mirroring the storage
#: sidecar's ``_TOOL_RESULT_ARTIFACT_MAX_BYTES``. A single spool may never
#: exceed this because the repository would refuse the row.
_SIDECAR_ARTIFACT_MAX_BYTES = 16 * _MIB

#: Recognisable temp-file names so abandoned spools can be reclaimed without
#: ever touching a user project or another component's temp files.
SPOOL_FILE_PREFIX = "tofu-tool-result-"
SPOOL_FILE_SUFFIX = ".spool"

_SPOOL_FILE_MODE = 0o600


def resolve_artifact_quota_bytes(
    environment: Mapping[str, str] | None = None,
    *,
    snapshot: Any | None = None,
) -> int:
    """Return the hard per-artifact spool cap from the launch resource budget.

    One spilled tool result can never exceed what a single storage RPC admits
    (``TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB``), and the sidecar refuses rows over
    16 MiB, so the resolved value is clamped to that ceiling. Operators may
    tighten it through the same environment override; malformed values fall
    back to the probed default.
    """
    mib = resolve_resource_budget(
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB',
        environment,
        minimum=1,
        maximum=_SIDECAR_ARTIFACT_MAX_BYTES // _MIB,
        snapshot=snapshot,
    )
    return min(mib * _MIB, _SIDECAR_ARTIFACT_MAX_BYTES)


def resolve_task_quota_bytes(
    environment: Mapping[str, str] | None = None,
    *,
    snapshot: Any | None = None,
) -> int:
    """Return the hard per-task aggregate spool cap from the launch budget.

    Transient spool files are reconstructible staging, not durable state, so
    the existing reconstructible-staging budget (``TOFU_BROWSER_STAGING_MAX_MIB``)
    is the closest launch seam: all spilled results of one task share it.
    """
    mib = resolve_resource_budget(
        'TOFU_BROWSER_STAGING_MAX_MIB',
        environment,
        minimum=1,
        maximum=4096,
        snapshot=snapshot,
    )
    return mib * _MIB


class TaskArtifactBudget:
    """Shared, thread-safe byte budget across one task's artifact spools.

    The per-task seam is meaningful only when every writer of a task shares
    one instance; a writer without a shared budget falls back to a private
    budget with the same resolved limit, so a single writer can still never
    exceed the task ceiling.
    """

    def __init__(self, limit_bytes: int) -> None:
        limit = int(limit_bytes)
        if limit <= 0:
            raise ValueError('task artifact budget must be positive')
        self._limit = limit
        self._lock = threading.Lock()
        self._used = 0

    @property
    def limit_bytes(self) -> int:
        return self._limit

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    def reserve(self, count: int) -> bool:
        """Atomically reserve ``count`` bytes, or reserve nothing on failure."""
        count = int(count)
        if count < 0:
            raise ValueError('reservation must be non-negative')
        with self._lock:
            if self._used + count > self._limit:
                return False
            self._used += count
            return True

    def release(self, count: int) -> None:
        count = int(count)
        if count < 0:
            raise ValueError('release must be non-negative')
        with self._lock:
            self._used = max(0, self._used - count)


@dataclass(frozen=True)
class ToolResultArtifact:
    """Model-facing outcome of finalizing an incremental result writer.

    ``sha256`` and ``size_bytes`` describe the full retained content when the
    result was persisted or inlined; a degraded result has no durable identity
    and therefore reports ``sha256=None`` with ``degraded=True``.
    """

    text: str
    artifact_ref: str | None
    sha256: str | None
    size_bytes: int
    spilled: bool
    truncated: bool
    complete: bool
    degraded: bool
    degraded_reason: str = ""


def _coerce_text(chunk: Any) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


class ToolResultArtifactWriter:
    """Incrementally spool one tool result and finalize it to a bounded form.

    Constructor parameters are the fault-injection and resource-budget seams;
    every one is optional and resolves to a launch-probed default when omitted.
    """

    def __init__(
        self,
        *,
        user_id: int,
        threshold_chars: int | None = None,
        head_chars: int | None = None,
        tail_chars: int | None = None,
        artifact_quota_bytes: int | None = None,
        task_quota_bytes: int | None = None,
        task_budget: TaskArtifactBudget | None = None,
        repository: Any | None = None,
        spool_dir: str | None = None,
        environment: Mapping[str, str] | None = None,
        deadline_seconds: float = 5.0,
    ) -> None:
        owner = int(user_id)
        if owner <= 0:
            raise ValueError('user_id must be a positive repository owner')
        self.user_id = owner

        threshold = int(threshold_chars or MAX_COMMAND_OUTPUT)
        if threshold < 1:
            threshold = int(MAX_COMMAND_OUTPUT)
        self.threshold_chars = threshold
        self.head_chars = (
            max(1, int(head_chars))
            if head_chars is not None else max(1, threshold * 3 // 4))
        self.tail_chars = (
            max(1, int(tail_chars))
            if tail_chars is not None else max(1, threshold // 4))

        resolved_artifact = (
            int(artifact_quota_bytes)
            if artifact_quota_bytes is not None and int(artifact_quota_bytes) > 0
            else resolve_artifact_quota_bytes(environment))
        resolved_task = (
            int(task_quota_bytes)
            if task_quota_bytes is not None and int(task_quota_bytes) > 0
            else resolve_task_quota_bytes(environment))
        self.artifact_quota_bytes = max(1, resolved_artifact)
        self.task_quota_bytes = max(1, resolved_task)
        self._task_budget = (
            task_budget
            if task_budget is not None
            else TaskArtifactBudget(self.task_quota_bytes))
        self._repository = repository
        self._spool_dir = spool_dir or tempfile.gettempdir()
        self._deadline_seconds = deadline_seconds

        # Lifecycle state, initialised first so __del__ is safe on a partial
        # construction (e.g. owner validation raised above already returned).
        self._buffered: list[str] = []
        self._buffered_chars = 0
        self._head_parts: list[str] = []
        self._head_len = 0
        self._tail_parts: list[str] = []
        self._tail_len = 0
        self._spool: Any | None = None
        self._spool_path: str | None = None
        self._spooled = False
        self._accepted_chars = 0
        self._accepted_bytes = 0
        self._hasher = hashlib.sha256()
        self._reserved_bytes = 0
        self._degraded = False
        self._degraded_reason = ""
        self._finalized = False
        self._discarded = False
        self._result: ToolResultArtifact | None = None

    # ── Observability ────────────────────────────────────────────────────
    @property
    def finalized(self) -> bool:
        return self._finalized

    @property
    def discarded(self) -> bool:
        return self._discarded

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def spooled(self) -> bool:
        return self._spooled

    # ── Streaming ────────────────────────────────────────────────────────
    def write(self, chunk: Any) -> bool:
        """Append one text chunk. Returns False when the chunk was rejected.

        A chunk is rejected once the writer is finalized/discarded, or once a
        hard quota or disk failure degraded retention. Rejection never raises:
        cancellation and overflow flows must be able to keep draining a source
        without unwinding the tool.
        """
        if self._finalized or self._degraded:
            return False
        text = _coerce_text(chunk)
        if not text:
            return True
        encoded = text.encode("utf-8", errors="replace")
        nbytes = len(encoded)
        if not self._reserve(nbytes):
            self._degrade("quota")
            return False
        self._hasher.update(encoded)
        self._accepted_chars += len(text)
        self._accepted_bytes += nbytes
        self._append_head(text)
        self._append_tail(text)
        if not self._spooled:
            self._buffered.append(text)
            self._buffered_chars += len(text)
            if self._buffered_chars > self.threshold_chars:
                self._spill()
        else:
            try:
                self._spool.write(text)
            except OSError as exc:
                logger.warning('[ToolResultWriter] spool write failed: %s', exc)
                self._degrade("disk")
                return False
        return True

    def finalize(self, *, complete: bool = True) -> ToolResultArtifact:
        """Finalize the buffered/spooled result exactly once.

        ``complete=False`` records a partial (cancellation) finalization: the
        retained content is still persisted/inlined so a cancelled tool result
        can be read back, but the outcome is flagged partial.
        """
        if self._result is not None:
            return self._result
        if self._discarded:
            raise RuntimeError('ToolResultArtifactWriter was discarded')
        try:
            if self._degraded:
                result = self._finalize_degraded(complete)
            elif not self._spooled:
                result = self._finalize_inline(complete)
            else:
                result = self._finalize_spilled(complete)
        finally:
            self._cleanup_spool()
            self._release_reservation()
            self._buffered = []
            self._buffered_chars = 0
            self._finalized = True
        self._result = result
        return result

    def finalize_partial(self) -> ToolResultArtifact:
        """Finalize as a partial (cancelled) result."""
        return self.finalize(complete=False)

    def discard(self) -> None:
        """Abandon the writer without finalizing; remove its temp spool."""
        if self._finalized:
            return
        self._cleanup_spool()
        self._release_reservation()
        self._buffered = []
        self._buffered_chars = 0
        self._finalized = True
        self._discarded = True

    # ── Context manager (exception cleanup) ──────────────────────────────
    def __enter__(self) -> ToolResultArtifactWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and not self._finalized:
            self.discard()
        return False

    def __del__(self) -> None:
        try:
            self._cleanup_spool()
            self._release_reservation()
        except Exception as e:
            logger.debug('tool result artifact destructor cleanup failed: %s', e)

    # ── Internals ────────────────────────────────────────────────────────
    def _reserve(self, nbytes: int) -> bool:
        if self._accepted_bytes + nbytes > self.artifact_quota_bytes:
            return False
        if not self._task_budget.reserve(nbytes):
            return False
        self._reserved_bytes += nbytes
        return True

    def _release_reservation(self) -> None:
        if self._reserved_bytes:
            try:
                self._task_budget.release(self._reserved_bytes)
            finally:
                self._reserved_bytes = 0

    def _degrade(self, reason: str) -> None:
        if self._degraded:
            return
        self._degraded = True
        self._degraded_reason = str(reason or "")
        self._cleanup_spool()
        self._release_reservation()

    def _spill(self) -> None:
        fd = None
        spool = None
        path = None
        try:
            fd, path = tempfile.mkstemp(
                prefix=SPOOL_FILE_PREFIX, suffix=SPOOL_FILE_SUFFIX,
                dir=self._spool_dir)
            try:
                if hasattr(os, 'fchmod'):
                    os.fchmod(fd, _SPOOL_FILE_MODE)
            except OSError as exc:
                logger.debug('[ToolResultWriter] fchmod failed: %s', exc)
            spool = os.fdopen(
                fd, "w+", encoding="utf-8", errors="replace", newline="")
            fd = None
            for part in self._buffered:
                spool.write(part)
            spool.flush()
            self._spool = spool
            self._spool_path = path
            self._buffered = []
            self._buffered_chars = 0
            self._spooled = True
        except OSError as exc:
            logger.warning('[ToolResultWriter] spool setup failed: %s', exc)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if spool is not None:
                try:
                    spool.close()
                except OSError:
                    pass
            if path is not None:
                try:
                    os.remove(path)
                except OSError:
                    pass
            self._degrade("disk")

    def _cleanup_spool(self) -> None:
        spool = self._spool
        self._spool = None
        if spool is not None:
            try:
                spool.close()
            except OSError:
                pass
        if self._spool_path is not None:
            try:
                os.remove(self._spool_path)
            except OSError:
                pass
            self._spool_path = None

    def _append_head(self, text: str) -> None:
        if self._head_len >= self.head_chars:
            return
        take = text[: self.head_chars - self._head_len]
        self._head_parts.append(take)
        self._head_len += len(take)

    def _append_tail(self, text: str) -> None:
        self._tail_parts.append(text)
        self._tail_len += len(text)
        while self._tail_len > self.tail_chars and self._tail_parts:
            overflow = self._tail_len - self.tail_chars
            first = self._tail_parts[0]
            if len(first) <= overflow:
                self._tail_len -= len(first)
                self._tail_parts.pop(0)
            else:
                self._tail_parts[0] = first[overflow:]
                self._tail_len = self.tail_chars

    def _head_text(self) -> str:
        return "".join(self._head_parts)

    def _tail_text(self) -> str:
        return "".join(self._tail_parts)

    def _preview_text(self) -> str:
        return (
            self._head_text()
            + f"\n\n… [output truncated: {self._accepted_chars:,} chars total] …\n\n"
            + self._tail_text())

    def _repository_client(self) -> Any:
        if self._repository is not None:
            return self._repository
        from lib.tool_result_artifacts import ToolResultArtifactRepository
        return ToolResultArtifactRepository(deadline_seconds=self._deadline_seconds)

    def _finalize_inline(self, complete: bool) -> ToolResultArtifact:
        text = "".join(self._buffered)
        return ToolResultArtifact(
            text=text,
            artifact_ref=None,
            sha256=self._hasher.hexdigest(),
            size_bytes=self._accepted_bytes,
            spilled=False,
            truncated=False,
            complete=complete,
            degraded=False,
        )

    def _finalize_spilled(self, complete: bool) -> ToolResultArtifact:
        try:
            self._spool.flush()
            self._spool.seek(0)
            full = self._spool.read()
        except OSError as exc:
            logger.warning('[ToolResultWriter] spool read failed: %s', exc)
            self._degraded = True
            self._degraded_reason = "disk"
            return self._finalize_degraded(complete)
        sha256 = self._hasher.hexdigest()
        ref = None
        degraded = False
        reason = ""
        try:
            result = self._repository_client().put(
                user_id=self.user_id, content=full, media_type="text/plain")
            ref = str((result or {}).get("artifactRef") or "")
        except Exception as exc:  # storage failure must degrade, never raise
            logger.warning('[ToolResultWriter] artifact persistence failed: %s',
                           exc)
            degraded = True
            reason = "persist"
        if not ref and not degraded:
            degraded = True
            reason = "persist"
        return ToolResultArtifact(
            text=self._preview_text(),
            artifact_ref=ref or None,
            sha256=None if degraded else sha256,
            size_bytes=self._accepted_bytes,
            spilled=True,
            truncated=True,
            complete=complete,
            degraded=degraded,
            degraded_reason=reason,
        )

    def _finalize_degraded(self, complete: bool) -> ToolResultArtifact:
        if self._accepted_chars <= self.threshold_chars:
            text = "".join(self._buffered)
            truncated = False
        else:
            text = self._preview_text()
            truncated = True
        return ToolResultArtifact(
            text=text,
            artifact_ref=None,
            sha256=None,
            size_bytes=self._accepted_bytes,
            spilled=self._spooled,
            truncated=truncated,
            complete=complete,
            degraded=True,
            degraded_reason=self._degraded_reason,
        )


def cleanup_abandoned_spools(
    *,
    spool_dir: str | None = None,
    max_age_seconds: float = 3600.0,
    now: float | None = None,
) -> int:
    """Remove stale temp spool files left behind by a crashed process.

    Only files matching this module's own ``SPOOL_FILE_PREFIX``/``SUFFIX`` in
    the system temp directory are touched, so user projects and other
    components are never at risk.
    """
    directory = spool_dir or tempfile.gettempdir()
    cutoff = (now if now is not None else time.time()) - max(
        0.0, float(max_age_seconds))
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for name in names:
        if not (name.startswith(SPOOL_FILE_PREFIX)
                and name.endswith(SPOOL_FILE_SUFFIX)):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


__all__ = [
    "SPOOL_FILE_PREFIX", "SPOOL_FILE_SUFFIX", "TaskArtifactBudget",
    "ToolResultArtifact", "ToolResultArtifactWriter",
    "cleanup_abandoned_spools", "resolve_artifact_quota_bytes",
    "resolve_task_quota_bytes",
]
