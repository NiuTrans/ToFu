"""Race-safe commit of translated content into ``conversations.messages``.

Endpoint mode spawns multiple auto-translate threads in parallel for one
conversation (one per planner + each worker iteration). A naive
read-modify-write on the full ``messages`` JSON lets the later writer
clobber earlier translations. Two layers of protection:

1. Per-conversation in-process ``threading.Lock`` (only one commit
   touches a given conv row at a time within this worker).
2. Inside the lock, a CAS loop on ``rev`` (the server-issued monotonic
   message-version bumped by conversations_rev_bump_trg) so we also survive
   concurrent writes from OTHER paths (frontend sync, save_conv, the terminal
   task sync, _sync_endpoint_turns_to_conversation). rev is strictly better
   than updated_at here: two writers that read the same row in the same
   millisecond no longer both pass the guard (RENDER_CONTRACT Phase 4 W6).
"""

import json
import threading
import time

from lib.database import DOMAIN_CHAT, json_dumps_pg
from lib.log import get_logger

from .constants import DEFAULT_USER_ID

logger = get_logger(__name__)


# ── Per-conversation commit serialization ──
_commit_locks_lock = threading.Lock()
_commit_locks: dict[str, threading.Lock] = {}


def _get_commit_lock(conv_id: str) -> threading.Lock:
    """Return a shared lock for serializing translate commits on one conv."""
    with _commit_locks_lock:
        lk = _commit_locks.get(conv_id)
        if lk is None:
            lk = threading.Lock()
            _commit_locks[conv_id] = lk
        return lk


def _stamp_segment_translations(msg, segment_translations):
    """Stamp per-round translated Chinese onto ``msg['segments']`` by llmRound.

    ``segment_translations`` is ``{round_num: 中文}``. Each non-deliverable
    ``text`` segment whose ``llmRound`` matches a key gets ``translatedText``.
    A no-op when the message carries no segments (pre-v36 row) or none match.
    """
    segs = msg.get('segments')
    if not isinstance(segs, list) or not segs:
        return
    stamped = 0
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        if seg.get('type') != 'text' or seg.get('deliverable'):
            continue
        lr = seg.get('llmRound')
        if lr is None:
            continue
        txt = segment_translations.get(lr)
        if txt and txt.strip():
            seg['translatedText'] = txt
            stamped += 1
    if stamped:
        logger.debug('[Translate] stamped translatedText on %d/%d segments',
                     stamped, len(segs))


def _commit_translation_to_db(conv_id, msg_idx, field, translated_text,
                              original_text=None, model=None, msg_id=None,
                              segment_translations=None, fallback_segments=None):
    """Write translated content directly into the conversation's messages in DB.

    ``segment_translations`` (optional): a ``{round_num: 中文}`` map from the
    incremental per-round translator. When present, each non-deliverable text
    segment of ``msg['segments']`` whose ``llmRound`` matches a key is stamped
    with ``translatedText`` so the settled segment-timeline render shows the
    translated narration in place (interleaved with its tools), exactly like
    the streaming preview. A pure projection co-located on the authoritative
    segment — parallel to ``translatedContent`` beside ``content``; the
    ``translatedContent`` blob semantics are unchanged.

    See module docstring for the race-safety rationale.
    """
    if not conv_id:
        logger.debug('[Translate] commit: missing conv_id — skipping')
        return

    lock = _get_commit_lock(conv_id)
    with lock:
        _commit_translation_inner(conv_id, msg_idx, field, translated_text,
                                  original_text=original_text, model=model,
                                  msg_id=msg_id,
                                  segment_translations=segment_translations,
                                  fallback_segments=fallback_segments)


def _commit_translation_inner(conv_id, msg_idx, field, translated_text,
                              original_text=None, model=None, msg_id=None,
                              segment_translations=None, fallback_segments=None):
    """CAS-retry body of _commit_translation_to_db (caller holds conv lock).

    Resolution order for the target message:
      1. msg_id (stable UUID) — preferred, robust against concurrent inserts
      2. msg_idx (position) — legacy path, only used when id missing or stale
      3. content match against original_text — final fallback for in-flight
         tasks that pre-date the id-aware translate flow
    """
    from lib.database import get_thread_db

    MAX_CAS_ATTEMPTS = 5
    last_err = None
    for attempt in range(MAX_CAS_ATTEMPTS):
        try:
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=?',
                (conv_id, DEFAULT_USER_ID)
            ).fetchone()
            if not row:
                logger.warning('[Translate] commit: conv=%s not found — skipping',
                               conv_id[:8])
                return

            messages = json.loads(row['messages'] or '[]')
            row['updated_at']
            # CAS token: rev (RENDER_CONTRACT Phase 4 W6). The trigger bumps rev
            # on every messages change, so the terminal sync / a sibling
            # translate thread advancing rev between our SELECT and UPDATE makes
            # us MISS (re-read + retry) rather than clobber. updated_at is still
            # stamped in SET for freshness but is no longer the CAS token.
            prev_rev = row['rev']

            # Resolution: id → idx → content. ID lookup is index-free and
            # the canonical path; idx is a legacy position fallback.
            msg = None
            resolved_idx = None
            resolved_via = None
            if msg_id:
                for i, candidate in enumerate(messages):
                    if isinstance(candidate, dict) and candidate.get('_msgId') == msg_id:
                        msg = candidate
                        resolved_idx = i
                        resolved_via = 'msgId'
                        break
            if msg is None and msg_idx is not None:
                try:
                    idx = int(msg_idx)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[translate] _commit_translation_inner caught %s: %s', type(_e_audit).__name__, _e_audit)
                    idx = -1
                if 0 <= idx < len(messages):
                    msg = messages[idx]
                    resolved_idx = idx
                    resolved_via = 'msgIdx'
            if msg is None and original_text:
                _orig_stripped = original_text.strip()[:200]
                for i, candidate in enumerate(reversed(messages)):
                    if not isinstance(candidate, dict):
                        continue
                    _cand_content = (candidate.get('content') or '').strip()[:200]
                    if _cand_content and _cand_content == _orig_stripped:
                        msg = candidate
                        resolved_idx = len(messages) - 1 - i
                        resolved_via = 'content'
                        logger.info('[Translate] commit: resolved by content match for conv=%s msgId=%s '
                                    '(msg_idx=%s out of range, len=%d)',
                                    conv_id[:8], (msg_id or '')[:8] or '-',
                                    msg_idx, len(messages))
                        break
            if msg is None:
                logger.warning('[Translate] commit: target message not found for conv=%s '
                               'msg_idx=%s msgId=%s len=%d — dropping translation',
                               conv_id[:8], msg_idx, (msg_id or '')[:8] or '-',
                               len(messages))
                return
            idx = resolved_idx if resolved_idx is not None else (
                int(msg_idx) if msg_idx is not None else -1
            )
            # Backfill the message's stable id if the caller passed one and
            # the message lacks it (e.g. translation started before the id
            # backfill landed).  This makes future PATCHes id-addressable.
            if msg_id and not msg.get('_msgId'):
                msg['_msgId'] = msg_id

            if field == 'translatedContent':
                msg['translatedContent'] = translated_text
                msg['_showingTranslation'] = True
                msg['_translateDone'] = True
                if model:
                    msg['_translateModel'] = model
                # ★ Per-round carry to the settled segment timeline: stamp each
                #   non-deliverable text segment with its translated Chinese,
                #   keyed by llmRound ≡ round_num (exact — never text-equality,
                #   which whitespace-normalization can miss and identical
                #   narration can collide). Deliverable/terminal segments are
                #   excluded from the timeline (rendered via translatedContent),
                #   so they need nothing.
                if segment_translations:
                    # ★ SELF-HEAL (SSOT ordering guarantee): the stamp is a
                    #   no-op when the resolved DB message carries no
                    #   `segments` — which happens when this commit raced
                    #   ahead of (or the frontend row-write CAS beat)
                    #   _sync_result_to_conversation, the reported 0/N bug. If
                    #   the caller handed the authoritative thin segments
                    #   (task['segments'] captured at finalize), splice them
                    #   onto the message in THIS SAME CAS write so the stamp
                    #   has something to land on. Gated on a non-empty map so
                    #   a plain translatedContent commit never fabricates
                    #   segments; segments are backend-authoritative so this
                    #   is not a second source of truth. `updated_at` still
                    #   bumps below (segments are new state here, unlike the
                    #   byte-identical save_conv preserve merge).
                    _existing_segs = msg.get('segments')
                    if (not (isinstance(_existing_segs, list) and _existing_segs)
                            and isinstance(fallback_segments, list)
                            and fallback_segments):
                        msg['segments'] = fallback_segments
                        logger.info('[Translate] commit: DB msg had no segments '
                                    '— spliced %d authoritative segments before '
                                    'stamp (conv=%s)', len(fallback_segments),
                                    conv_id[:8])
                    _stamp_segment_translations(msg, segment_translations)
            elif field == 'content':
                if not msg.get('originalContent'):
                    msg['originalContent'] = msg.get('content', '')
                msg['content'] = translated_text
            else:
                msg[field] = translated_text

            new_updated = int(time.time() * 1000)
            # CAS — only update if rev hasn't advanced since we read it.
            # If another writer (frontend sync / terminal sync / other translate
            # thread) changed messages in the meantime, the trigger bumped rev,
            # so the row count will be 0 and we'll re-read and retry.
            # ``rev`` is the WHERE token only — NEVER written in SET (the
            # conversations_rev_bump_trg trigger is the sole bumper).
            # NOTE: we call db.execute directly (not db_execute_with_retry)
            # because we need access to ``rowcount`` for the CAS check —
            # the retry helper returns None.  The outer for-loop provides
            # the retry semantics (including CAS-miss retries).
            cur = db.execute(
                'UPDATE conversations SET messages=?, updated_at=? '
                'WHERE id=? AND user_id=? AND rev=?',
                (json_dumps_pg(messages), new_updated,
                 conv_id, DEFAULT_USER_ID, prev_rev)
            )
            db.commit()
            rowcount = getattr(cur, 'rowcount', None)
            if rowcount == 0:
                # CAS miss — someone else wrote first.  Retry with fresh read.
                logger.info('[Translate] commit CAS miss on conv=%s msg=%d '
                            '(attempt %d/%d) — retrying',
                            conv_id[:8], idx, attempt + 1, MAX_CAS_ATTEMPTS)
                # Small sleep to avoid hot-spinning on a contended row.
                time.sleep(0.05 * (attempt + 1))
                continue
            logger.info('[Translate] Committed %s to conv=%s msg=%d via=%s '
                        '(%d chars, attempt=%d)',
                        field, conv_id[:8], idx, resolved_via or 'idx',
                        len(translated_text), attempt + 1)
            # Event-driven cross-device sync: the translated body changed, so
            # push the post-write rev → a sibling tab with this conv open shows
            # the translation without a manual refresh.
            try:
                from lib.conversations import notify_conv_changed
                _tr_rev_row = db.execute(
                    'SELECT rev FROM conversations WHERE id=? AND user_id=?',
                    (conv_id, DEFAULT_USER_ID)).fetchone()
                notify_conv_changed(conv_id, rev=(_tr_rev_row[0] if _tr_rev_row else None))
            except Exception as _ne:
                logger.debug('[Translate] conv-changed notify skipped conv=%s: %s',
                             conv_id[:8], _ne)
            return
        except Exception as e:
            last_err = e
            logger.warning('[Translate] commit attempt %d/%d failed for '
                           'conv=%s msg=%s: %s',
                           attempt + 1, MAX_CAS_ATTEMPTS, conv_id[:8],
                           msg_idx, e)
            time.sleep(0.1 * (attempt + 1))
    logger.error('[Translate] commit gave up after %d attempts for conv=%s msg=%s: %s',
                 MAX_CAS_ATTEMPTS, conv_id[:8], msg_idx, last_err,
                 exc_info=bool(last_err))
