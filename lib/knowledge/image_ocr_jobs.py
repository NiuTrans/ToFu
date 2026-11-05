"""Owned background OCR for attachment images indexed before their text.

Responsibility
--------------
A tool-result image must be durable before its receipt is published, but its
OCR text is only a search proxy. ``store.add_document(..., defer_image_ocr=True)``
commits the original, its asset row and its visual proxy chunk without OCR
text, then schedules one job per new asset here. A job reads the stored
original, runs OCR in an owned child (``lib.knowledge.ocr``) and publishes
``ocr_text`` with the rebuilt proxy chunk through ``knowledge.asset.update``,
the command visual enrichment uses. The published index equals synchronous
indexing of the same bytes.

Lifecycle
---------
``schedule`` starts one carrier thread per accepted job, with no count limit,
and coalesces a job already in flight for the same owner and asset. ``stop``
refuses new jobs, cancels running ones (the OCR child is terminated and joined
and nothing is written afterwards), joins every carrier and reopens once all
have exited. A process exit loses only in-flight derived text: the record keeps
empty OCR text, as after an OCR failure, until ``reindex_document`` recomputes
it. ``snapshot`` exposes counters without owner identifiers.

Dependencies
------------
``lib.knowledge.store``/``repository`` for durable state, ``lib.knowledge.ocr``
for the process boundary.
"""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import threading
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)


class _Cancelled(Exception):
    """The owner stopped while this job was running."""


def _index_asset_ocr(owner_user_id: int, asset_id: str,
                     cancelled: Callable[[], None]) -> str:
    from . import store
    from .assets import proxy_text
    from .ocr import OcrWorkerError, recognize_image
    from .repository import KnowledgeRepository

    loaded = store.read_asset(asset_id, user_id=owner_user_id)
    if loaded is None:
        return 'missing'
    row, raw = loaded
    started = time.monotonic()
    try:
        text = recognize_image(raw, str(row.get('mime_type') or ''), cancelled=cancelled)
    except OcrWorkerError as exc:
        # Best-effort, as for synchronous indexing: the proxy keeps no OCR line.
        logger.debug('[Knowledge] attachment OCR unavailable for asset %s: %s', asset_id, exc)
        return 'failed'
    if not text:
        return 'empty'
    cancelled()
    # Rebuild the proxy from the committed row so concurrent field edits survive.
    current = store.get_asset(asset_id, user_id=owner_user_id)
    if current is None:
        return 'missing'
    document_name = str(current.get('document_name') or 'document')
    content = proxy_text(document_name, {**current, 'ocr_text': text})
    section = str(current.get('caption') or '') or 'Visual evidence'
    asset_key = hashlib.sha256(asset_id.encode('utf-8')).hexdigest()
    updated = KnowledgeRepository(owner_user_id).update_asset(
        asset_id,
        command_id=f'knowledge.asset.ocr:{owner_user_id}:{uuid.uuid4().hex}:{asset_key}',
        updates={'ocr_text': text},
        chunk_content=content,
        chunk_search_text=store._index_text(document_name, section, content, cap=None),
    )
    if not updated:
        return 'missing'
    logger.info('[Knowledge] OCR indexed %s: %d chars in %.1fs after its receipt',
                document_name, len(text), time.monotonic() - started)
    return 'published'


class AttachmentImageOcr:
    """One carrier per accepted job; ``stop`` cancels and joins all of them."""

    def __init__(self, index: Callable[[int, str, Callable[[], None]], str] = _index_asset_ocr):
        self._index = index
        self._lock = threading.Lock()
        self._jobs: dict[tuple[int, str], threading.Thread] = {}
        self._stopping = threading.Event()
        self._counts = dict.fromkeys(
            ('accepted', 'published', 'empty', 'missing', 'failed', 'cancelled'), 0)

    def schedule(self, *, user_id: int, asset_id: str) -> bool:
        key = (require_user_id(user_id, context='attachment OCR owner'), str(asset_id))
        with self._lock:
            if self._stopping.is_set() or key in self._jobs:
                return False
            carrier = threading.Thread(
                target=self._carry, args=key, name='knowledge-attachment-ocr', daemon=True)
            self._jobs[key] = carrier
            try:
                carrier.start()
            except BaseException:
                del self._jobs[key]
                raise
            self._counts['accepted'] += 1
        return True

    def _cancelled(self) -> None:
        if self._stopping.is_set():
            raise _Cancelled()

    def _carry(self, owner_user_id: int, asset_id: str) -> None:
        outcome = 'failed'
        try:
            outcome = self._index(owner_user_id, asset_id, self._cancelled)
        except _Cancelled:
            outcome = 'cancelled'
        except Exception as exc:
            logger.warning('[Knowledge] attachment OCR for asset %s failed: %s',
                           asset_id, exc, exc_info=True)
        finally:
            with self._lock:
                self._jobs.pop((owner_user_id, asset_id), None)
                self._counts[outcome] += 1

    def stop(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._stopping.set()
            carriers = tuple(self._jobs.values())
        for carrier in carriers:
            carrier.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if self._jobs or any(carrier.is_alive() for carrier in carriers):
                return False
            self._stopping.clear()
            return True

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {'running': len(self._jobs), **self._counts}


_ATTACHMENT_OCR = AttachmentImageOcr()


def schedule_attachment_image_ocr(*, user_id: int, asset_id: str) -> bool:
    return _ATTACHMENT_OCR.schedule(user_id=user_id, asset_id=asset_id)


def stop_attachment_image_ocr(timeout: float = 2.0) -> bool:
    return _ATTACHMENT_OCR.stop(timeout)


def attachment_image_ocr_snapshot() -> dict[str, int]:
    return _ATTACHMENT_OCR.snapshot()


__all__ = [
    'AttachmentImageOcr', 'attachment_image_ocr_snapshot',
    'schedule_attachment_image_ocr', 'stop_attachment_image_ocr',
]
