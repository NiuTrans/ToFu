"""Human-owned project watch list and bounded response trails.

Sidecar ``watch`` operations own all persistence. Open goals are injected
directly as standing intent; concerns and questions remain human-facing unless
explicitly promoted into the charter. Recurring synthesis is fingerprint-gated
and coalesced through a bounded background worker lane.
"""

from __future__ import annotations

import hashlib
import json
import time

from lib.conversations._bounded_lane import BoundedCoalescingLane
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.storage import get_storage_client
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

# Keep at most this many responses per item (bounded trail; pruned on insert).
_RESPONSES_KEEP = 100
# Soft cap on a human-authored item's text. A goal is injected into every
# sibling's prompt verbatim, so this is also the per-goal prompt-weight ceiling.
# NOT tied to project_charter._CONTENT_MAX_CHARS any more: a goal is no longer
# copied into that column, so there is no "adopt the other side" direction that
# could truncate, and the two texts are now genuinely independent settings.
_ITEM_TEXT_MAX = 8000
# Bounded response length.
_RESPONSE_MAX_CHARS = 2000

# Total budget for the [PROJECT GOALS] block. Goals ride EVERY turn of EVERY
# sibling conversation, so an unbounded lane would let one long paste tax the
# whole project forever. Oldest-first truncation is deliberate: the block states
# plainly when it elided goals rather than silently shipping a subset.
_GOALS_BLOCK_MAX_CHARS = 4000

VALID_KINDS = ('concern', 'question', 'goal')
VALID_STATUSES = ('open', 'resolved')

# The COMPUTED promotion states for concern/question (decision 4). Never
# persisted. A GOAL never has one of these — it is injected, or it is resolved.
PROMOTION_NONE = 'none'      # no promotion on record → offer "add to charter"
PROMOTION_ACTIVE = 'active'  # item text IS a live committed decision

_SYSTEM_PROMPTS = {
    'question': (
        'You are the project brain answering a specific QUESTION the human '
        'owner is tracking about their project. Answer it directly and '
        'concretely using ONLY the project state provided. If the state does '
        'not contain the answer, say so plainly — do NOT invent facts.'),
    'concern': (
        'You are the project brain addressing a CONCERN the human owner is '
        'tracking. Using ONLY the project state provided, assess whether the '
        'concern is being addressed, is at risk, or is currently a non-issue, '
        'and say why. Be concrete; if the state does not speak to it, say so.'),
    'goal': (
        'You are the project brain reporting on a GOAL the human owner is '
        'tracking. Using ONLY the project state provided, report concrete '
        'progress toward the goal and whether current in-flight work is '
        'aligned with it or drifting. If the state does not speak to it, say '
        'so plainly.'),
}
_COMMON_SUFFIX = (
    '\nBe concise and dense (2-4 sentences). No greetings or filler. Use the '
    'same language as the item text.')


# ══════════════════════════════════════════════════════════════════════
#  Human CRUD — the human authors / edits / resolves / deletes items
# ══════════════════════════════════════════════════════════════════════


def add_watch_item(project_path: str, kind: str, text: str, *, user_id: int,
                   created_by_conv: str = '') -> dict:
    """Create one validated human-authored watch item."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    text = (text or '').strip()[:_ITEM_TEXT_MAX]
    kind = (kind or 'concern').strip().lower()
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if kind not in VALID_KINDS:
        return {'ok': False, 'error': 'invalid kind'}
    if not text:
        return {'ok': False, 'error': 'empty text'}
    try:
        result = get_storage_client(write=True).command(
            'watch.mutate',
            {
                'action': 'add',
                'project_path': project_path,
                'user_id': int(user_id),
                'kind': kind,
                'text': text,
                'created_by_conv': created_by_conv or '',
            },
            f'watch.add:{int(user_id)}:{project_path}:{kind}:{short_id("wa_", 10)}',
        )
    except Exception as exc:
        logger.error(
            '[Watch] add failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}
    if result.get('ok'):
        audit_log(
            'watch_item_added',
            project_path=project_path,
            item_id=(result.get('item') or {}).get('item_id', ''),
            kind=kind,
        )
    return result


def edit_watch_item(item_id: str, *, user_id: int, text: str | None = None,
                    kind: str | None = None) -> dict:
    """Edit text and/or kind; a text change invalidates synthesis freshness."""
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        client = get_storage_client(write=True)
        current = client.query(
            'watch.get', {'item_id': item_id, 'user_id': int(user_id)})
        if not current:
            return {'ok': False, 'error': 'not found'}
        new_text = (
            current.get('text', '')
            if text is None
            else (text or '').strip()[:_ITEM_TEXT_MAX]
        )
        new_kind = (
            current.get('kind', 'concern')
            if kind is None
            else (kind or '').strip().lower()
        )
        if not new_text:
            return {'ok': False, 'error': 'empty text'}
        if new_kind not in VALID_KINDS:
            return {'ok': False, 'error': 'invalid kind'}
        result = client.command(
            'watch.edit',
            {
                'item_id': item_id,
                'user_id': int(user_id),
                'text': new_text,
                'kind': new_kind,
            },
            f'watch.edit:{item_id}:{short_id("we_", 10)}',
        )
    except Exception as exc:
        logger.error(
            '[Watch] edit failed item=%s: %s', item_id, exc, exc_info=True)
        return {'ok': False, 'error': str(exc)}
    if result.get('ok'):
        audit_log('watch_item_edited', item_id=item_id)
    return result


def set_watch_status(item_id: str, status: str, *, user_id: int) -> dict:
    """Set open/resolved; resolving a goal withdraws it from prompt injection."""
    status = (status or '').strip().lower()
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    if status not in VALID_STATUSES:
        return {'ok': False, 'error': 'invalid status'}
    try:
        result = get_storage_client(write=True).command(
            'watch.status',
            {'item_id': item_id, 'user_id': int(user_id), 'status': status},
            f'watch.status:{item_id}:{status}:{short_id("ws_", 8)}',
        )
    except Exception as exc:
        logger.error(
            '[Watch] status failed item=%s: %s', item_id, exc, exc_info=True)
        return {'ok': False, 'error': str(exc)}
    if result.get('ok'):
        audit_log('watch_item_status', item_id=item_id, status=status)
    return result


def delete_watch_item(item_id: str, *, user_id: int) -> dict:
    """Delete one watch item and its response trail atomically."""
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        result = get_storage_client(write=True).command(
            'watch.mutate',
            {'action': 'delete', 'item_id': item_id, 'user_id': int(user_id)},
            f'watch.delete:{item_id}',
        )
    except Exception as exc:
        logger.error(
            '[Watch] delete failed item=%s: %s', item_id, exc, exc_info=True)
        return {'ok': False, 'error': str(exc)}
    if result.get('ok'):
        audit_log('watch_item_deleted', item_id=item_id)
    return result

# ══════════════════════════════════════════════════════════════════════
#  Read — items + their append-only response trails
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
#  The COMPUTED promotion verdict — concern/question ONLY (decision 4)
# ══════════════════════════════════════════════════════════════════════

def _norm(text: str) -> str:
    """Normalize text for promotion-equality comparison.

    Strips outer whitespace and collapses every internal whitespace run
    (including newlines) to one space, so a reflowed paragraph still counts as
    the same item. Case is deliberately PRESERVED — capitalization carries
    meaning, and folding it would call two genuinely different texts equal.
    """
    return ' '.join((text or '').split())


def promotion_state(item: dict, charter: dict) -> dict:
    """Compute — never read — whether a concern/question is in the charter.

    The stored ``promoted`` boolean cannot answer this: it records that a
    promotion once happened, not that its effect survives. The charter can be
    deleted or the decision FIFO-evicted, and the boolean still reads 1 while
    nothing reaches the model. (Measured on the live project: promoted=1,
    read_charter() exists=False, injection block 0 bytes.)

    A ``goal`` ALWAYS returns ``none``: goals do not go through the charter at
    all (decision 1), so "is it promoted" is not a question about them. Their
    prompt presence is decided by :func:`render_goals_injection_block`, and the
    UI renders that as an injected/not-injected fact rather than a promotion.

    Returns ``{state, divergedSide}``. ``divergedSide`` is retained as an
    always-empty key so a stale frontend reading it gets a falsy value instead
    of ``undefined``. Pure; never raises.
    """
    kind = (item or {}).get('kind') or 'concern'
    if kind == 'goal':
        return {'state': PROMOTION_NONE, 'divergedSide': ''}
    item_text = (item or {}).get('text') or ''
    charter = charter or {}
    live_norms = []
    for d in (charter.get('decisions') or []):
        txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
        if _norm(txt):
            live_norms.append(_norm(txt))
    norm_item = _norm(item_text)
    # The bridge prefixes the committed text ("[Concern — promoted by owner] …"),
    # so containment — not equality — is the right test here.
    if norm_item and any(norm_item in live for live in live_norms):
        return {'state': PROMOTION_ACTIVE, 'divergedSide': ''}
    return {'state': PROMOTION_NONE, 'divergedSide': ''}


# ══════════════════════════════════════════════════════════════════════
#  The [PROJECT GOALS] prompt block — the ONE way a goal reaches agents
# ══════════════════════════════════════════════════════════════════════


def render_goals_injection_block(project_path: str, *, user_id: int) -> str:
    """Render open goals only; synthesized watch responses are never injected."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return ''
    try:
        items = get_storage_client().query(
            'watch.list',
            {
                'project_path': project_path,
                'user_id': int(user_id),
                'include_resolved': False,
                'response_limit': 1,
            },
        ).get('items', [])
    except Exception as exc:
        logger.warning(
            '[Watch] goals read failed proj=%.40r: %s', project_path, exc)
        return ''

    goals = [
        item for item in items
        if item.get('kind') == 'goal' and item.get('status') == 'open'
    ]
    goals.sort(key=lambda item: int(item.get('created_at') or 0))
    texts = [
        str(item.get('text') or '').strip()
        for item in goals
        if str(item.get('text') or '').strip()
    ]
    if not texts:
        return ''

    header = (
        '[PROJECT GOALS] — what the human owner wants this project to achieve. '
        'They set these directly; treat them as standing intent that outranks '
        'local convenience, and say so when a request conflicts with one.'
    )
    body: list[str] = []
    used = 0
    for text in texts:
        entry = f'  • {text}'
        if body and used + len(entry) > _GOALS_BLOCK_MAX_CHARS:
            break
        body.append(entry)
        used += len(entry)
    lines = [header, '', *body]
    elided = len(texts) - len(body)
    if elided:
        lines.append(
            f'  … and {elided} more goal(s) not shown '
            f'(block is capped at {_GOALS_BLOCK_MAX_CHARS} chars).'
        )
    return '\n'.join(lines)


def list_watch_items(project_path: str, *, user_id: int,
                     include_resolved: bool = True,
                     resp_limit: int = 20) -> dict:
    """Return watch items plus live promotion verdicts against one charter read."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return {'items': [], 'charterVersion': 0}
    response_limit = max(1, min(int(resp_limit or 20), _RESPONSES_KEEP))
    try:
        result = get_storage_client().query(
            'watch.list',
            {
                'project_path': project_path,
                'user_id': int(user_id),
                'include_resolved': bool(include_resolved),
                'response_limit': response_limit,
            },
        )
    except Exception as exc:
        logger.warning(
            '[Watch] list failed proj=%.40r: %s', project_path, exc)
        return {'items': [], 'charterVersion': 0}

    charter: dict = {}
    try:
        from lib.conversations.project_charter import read_charter
        charter = read_charter(project_path, user_id=user_id)
    except Exception as exc:
        logger.warning(
            '[Watch] charter read failed proj=%.40r: %s', project_path, exc)
    items = []
    for item in result.get('items') or []:
        projected = dict(item)
        verdict = promotion_state(projected, charter)
        projected['promotionState'] = verdict['state']
        projected['divergedSide'] = verdict['divergedSide']
        items.append(projected)
    return {
        'items': items,
        'charterVersion': int(charter.get('version') or 0),
    }

# ══════════════════════════════════════════════════════════════════════
#  Address — the brain synthesizes a recurring response per item
# ══════════════════════════════════════════════════════════════════════

def _item_fingerprint(item_text: str, pillar_state: dict) -> str:
    """Change key gating whether an item needs a fresh response: the item text
    (so an edit re-addresses) + the SAME coarse pillar fingerprint the status
    lane uses (so sibling progress re-addresses)."""
    from lib.conversations.project_status import _fingerprint as _pfp
    return f'{hash(item_text)}::{_pfp(pillar_state)}'


def generate_item_response(kind: str, item_text: str, pillar_state: dict) -> str:
    """Synthesize ONE response to a watch item from live pillar state via the
    cheap model. Returns '' on failure (caller keeps prior response)."""
    from lib.conversations.project_status import _build_synthesis_source
    source = _build_synthesis_source(pillar_state)
    if not (item_text or '').strip():
        return ''
    system = _SYSTEM_PROMPTS.get(kind, _SYSTEM_PROMPTS['concern']) + _COMMON_SUFFIX
    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': system},
                {'role': 'user',
                 'content': f'Project state:\n\n{source}\n\n'
                            f'The {kind} I am tracking: {item_text}\n\nResponse:'},
            ],
            max_tokens=700, temperature=0.3, capability='cheap',
            log_prefix='[Watch]',
        )
    except Exception as e:
        logger.warning('[Watch] synthesis failed after %.1fs: %s',
                       time.time() - started, e)
        return ''
    text = (content or '').strip()
    if len(text) > _RESPONSE_MAX_CHARS:
        text = text[:_RESPONSE_MAX_CHARS].rstrip() + '…'
    return text


def _watch_response_command_id(payload: dict) -> str:
    """Receipt id derived from the request content, never wall-clock ms.

    A millisecond key (``item:trigger:ts``) collided whenever two distinct
    responses landed in the same ms — the receipt layer then rejected the
    second as "command_id reused for a different request" and the caller saw
    a spurious persist failure instead of the CAS verdict.  It also failed to
    dedupe a genuine retry, which arrives at a later ms with identical
    content.  Hashing the canonical payload gives both properties: identical
    retries replay the stored receipt; distinct responses always run.
    """
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':'), default=str).encode('utf-8')
    ).hexdigest()
    return f"watch.response:{payload.get('item_id', '')}:{digest}"



def _persist_response(
    item_id: str,
    response: str,
    pillar_state: dict,
    trigger: str,
    *,
    user_id: int,
    fingerprint_guard: tuple[str, int, str] | None = None,
) -> dict | None:
    """Append one response and optionally advance freshness in one transaction."""
    payload = {
        'item_id': item_id,
        'user_id': int(user_id),
        'response': response,
        'pillar_state': pillar_state,
        'trigger': trigger or 'manual',
        'keep': _RESPONSES_KEEP,
        'fingerprint_guard': (
            list(fingerprint_guard) if fingerprint_guard else None
        ),
    }
    try:
        return get_storage_client(write=True).command(
            'watch.response.append',
            payload,
            _watch_response_command_id(payload),
        )
    except Exception as exc:
        logger.warning(
            '[Watch] response persist failed item=%s: %s', item_id, exc)
        return None


def address_watch_item(item_id: str, *, user_id: int, trigger: str = 'manual',
                       force: bool = False) -> dict | None:
    """Return the latest response, synthesizing only when its fingerprint moved."""
    if not item_id:
        return None
    try:
        client = get_storage_client()
        item = client.query(
            'watch.get', {
                'item_id': item_id,
                'user_id': int(user_id),
                'response_limit': 1,
            })
    except Exception as exc:
        logger.warning(
            '[Watch] address read failed item=%s: %s', item_id, exc)
        return None
    if not item:
        return None

    trail = item.get('responses') or []
    if item.get('status', 'open') != 'open' and not force:
        return trail[0] if trail else None

    from lib.conversations.project_status import collect_pillar_state
    project_path = item.get('project_path') or ''
    pillar_state = collect_pillar_state(project_path, user_id=user_id)
    fingerprint = _item_fingerprint(item.get('text') or '', pillar_state)
    stored_fingerprint = item.get('response_fingerprint') or ''
    if not force and stored_fingerprint == fingerprint:
        return trail[0] if trail else None

    response = generate_item_response(
        item.get('kind') or 'concern',
        item.get('text') or '',
        pillar_state,
    )
    if not response:
        return trail[0] if trail else None

    snapshot = _persist_response(
        item_id,
        response,
        pillar_state,
        trigger,
        user_id=user_id,
        fingerprint_guard=(
            stored_fingerprint,
            int(item.get('updated_at') or 0),
            fingerprint,
        ),
    )
    if snapshot and snapshot.get('conflict'):
        try:
            latest = client.query(
                'watch.get', {
                    'item_id': item_id,
                    'user_id': int(user_id),
                    'response_limit': 1,
                })
            latest_trail = (latest or {}).get('responses') or []
            return latest_trail[0] if latest_trail else None
        except Exception as exc:
            logger.debug(
                '[Watch] conflict winner read failed item=%s: %s',
                item_id, exc,
            )
            return None
    return snapshot


_BACKGROUND_WORKERS = 2
_BACKGROUND_CAPACITY = resolve_resource_budget(
    'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY', maximum=4096)


def _merge_background_trigger(_current: str, newest: str) -> str:
    return newest


def _consume_background_address(
    scope: tuple[int, str], trigger: str
) -> None:
    user_id, project_path = scope
    _address_open_items_blocking(project_path, trigger, user_id=user_id)


def _report_background_address_failure(
    scope: tuple[int, str], error: Exception
) -> None:
    logger.warning(
        '[Watch] background address failed proj=%.40r: %s',
        scope[1], error, exc_info=True)


_background_lane = BoundedCoalescingLane[tuple[int, str], str](
    name='project-watch',
    workers=_BACKGROUND_WORKERS,
    capacity=_BACKGROUND_CAPACITY,
    merge=_merge_background_trigger,
    consume=_consume_background_address,
    on_error=_report_background_address_failure,
)


def _schedule_background_address(
    project_path: str, trigger: str, *, user_id: int
) -> bool:
    scope = (int(user_id), project_path)
    return _background_lane.submit(scope, trigger)


def _wait_for_background_watch(timeout: float = 5.0) -> bool:
    """Wait for the watch refresh lane; lifecycle/test diagnostic seam."""
    return _background_lane.wait_idle(timeout)


def background_watch_lane_snapshot() -> dict[str, float | int | str]:
    """Operational counters for capacity, saturation, and coalescing."""
    return _background_lane.snapshot()


def address_open_items(project_path: str, *, user_id: int,
                       trigger: str = 'manual',
                       blocking: bool = True) -> None:
    """Refresh open items now, or coalesce the project onto a bounded worker."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return
    if blocking:
        _address_open_items_blocking(project_path, trigger, user_id=user_id)
    else:
        _schedule_background_address(project_path, trigger, user_id=user_id)


def _address_open_items_blocking(
    project_path: str, trigger: str, *, user_id: int
) -> None:
    try:
        items = get_storage_client().query(
            'watch.list',
            {
                'project_path': project_path,
                'user_id': int(user_id),
                'include_resolved': False,
                'response_limit': 1,
            },
        ).get('items', [])
    except Exception as exc:
        logger.warning(
            '[Watch] address list failed proj=%.40r: %s', project_path, exc)
        return
    for item in items:
        try:
            address_watch_item(
                item.get('item_id'), user_id=user_id, trigger=trigger)
        except Exception as exc:
            logger.debug(
                '[Watch] address item=%s skipped: %s',
                item.get('item_id'), exc,
            )

# ══════════════════════════════════════════════════════════════════════
#  Follow-up Q&A — the human's thread ON one response (Increment 2 slice)
# ══════════════════════════════════════════════════════════════════════

_FOLLOW_UP_SYSTEM = (
    'You are the project brain. The human owner is asking a FOLLOW-UP about '
    'ONE earlier response you gave on a {kind} they are tracking. Answer the '
    'follow-up directly and concretely using ONLY the project state provided. '
    'Stay consistent with the earlier response unless the state has moved — '
    'if it moved, say what changed. If the state does not contain the answer, '
    'say so plainly — do NOT invent facts.')



def answer_follow_up(item_id: str, question: str, *, user_id: int,
                     response_seq: int | None = None) -> dict:
    """Answer a human follow-up and append it to the same response trail."""
    question = (question or '').strip()
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    if not question:
        return {'ok': False, 'error': 'empty question'}
    try:
        item = get_storage_client().query(
            'watch.get',
            {
                'item_id': item_id,
                'user_id': int(user_id),
                'response_limit': _RESPONSES_KEEP,
            },
        )
    except Exception as exc:
        logger.warning(
            '[Watch] follow-up read failed item=%s: %s', item_id, exc)
        return {'ok': False, 'error': str(exc)}
    if not item:
        return {'ok': False, 'error': 'not found'}

    trail = item.get('responses') or []
    anchor = next(
        (
            response for response in trail
            if response_seq is not None
            and int(response.get('seq') or 0) == int(response_seq)
        ),
        None,
    )
    if anchor is None and response_seq is None:
        anchor = trail[0] if trail else None
    anchor_text = (anchor or {}).get('response') or ''
    anchor_seq = int((anchor or {}).get('seq') or 0)
    kind = item.get('kind') or 'concern'
    project_path = item.get('project_path') or ''

    from lib.conversations.project_status import (
        _build_synthesis_source,
        collect_pillar_state,
    )
    pillar_state = collect_pillar_state(project_path, user_id=user_id)
    source = _build_synthesis_source(pillar_state)
    system = _FOLLOW_UP_SYSTEM.format(kind=kind) + _COMMON_SUFFIX
    user = (
        f'Project state:\n\n{source}\n\n'
        f'The {kind} I am tracking: {item.get("text") or ""}\n\n'
        f'Your earlier response: {anchor_text or "(none yet)"}\n\n'
        f'My follow-up: {question}\n\nAnswer:'
    )
    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            max_tokens=700,
            temperature=0.3,
            capability='cheap',
            log_prefix='[Watch]',
        )
    except Exception as exc:
        logger.warning(
            '[Watch] follow-up synthesis failed after %.1fs: %s',
            time.time() - started, exc,
        )
        return {'ok': False, 'error': str(exc)}

    text = (content or '').strip()
    if not text:
        return {'ok': False, 'error': 'empty response'}
    if len(text) > _RESPONSE_MAX_CHARS:
        text = text[:_RESPONSE_MAX_CHARS].rstrip() + '…'
    evidence = dict(pillar_state) if isinstance(pillar_state, dict) else {}
    evidence['followUpQuestion'] = question
    evidence['anchorSeq'] = anchor_seq
    snapshot = _persist_response(
        item_id, text, evidence, 'follow_up', user_id=user_id)
    if not snapshot:
        return {'ok': False, 'error': 'persist failed'}
    return {'ok': True, 'response': snapshot}

# ══════════════════════════════════════════════════════════════════════
#  Promote-to-charter — concern/question ONLY (a goal never travels here)
# ══════════════════════════════════════════════════════════════════════

def _goal_summary(text: str) -> str:
    """One-line summary for a committed decision (its first line, bounded).

    ``commit_charter`` renders ONLY this line in the per-turn injection block
    (via ``_decision_headline``), so omitting it — as this bridge used to — left
    every promoted concern/question showing as a first line clipped mid-sentence
    by the generic fallback. The charter owns the ceiling; we import it rather
    than re-hardcoding 240.
    """
    from lib.conversations.project_charter import _SUMMARY_MAX_CHARS
    first = (text or '').strip().split('\n', 1)[0].strip()
    if len(first) > _SUMMARY_MAX_CHARS:
        first = first[:_SUMMARY_MAX_CHARS].rstrip() + '…'
    return first



def promote_watch_item(item_id: str, *, user_id: int,
                       updated_by_conv: str = '',
                       expected_version: int | None = None) -> dict:
    """Promote a concern/question into one charter invariant."""
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        client = get_storage_client(write=True)
        item = client.query(
            'watch.get', {'item_id': item_id, 'user_id': int(user_id)})
    except Exception as exc:
        logger.error(
            '[Watch] promote read failed item=%s: %s',
            item_id, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}
    if not item:
        return {'ok': False, 'error': 'not found'}

    project_path = item.get('project_path') or ''
    kind = item.get('kind') or 'concern'
    item_text = item.get('text') or ''
    if kind == 'goal':
        return {'ok': False, 'error': 'goal_not_promotable'}

    from lib.conversations.project_charter import commit_charter
    label = {'concern': 'Concern', 'question': 'Question'}.get(
        kind, 'Watch item')
    result = commit_charter(
        project_path,
        user_id=user_id,
        add_decision=f'[{label} — promoted by owner] {item_text}',
        decision_kind='invariant',
        summary=_goal_summary(item_text),
        updated_by_conv=updated_by_conv or '',
        expected_version=expected_version,
    )
    if not result.get('ok'):
        return result
    try:
        client.command(
            'watch.promote',
            {'item_id': item_id, 'user_id': int(user_id)},
            f'watch.promote:{item_id}:{result.get("version")}',
        )
    except Exception as exc:
        logger.debug(
            '[Watch] promoted audit flag skipped item=%s: %s', item_id, exc)
    audit_log(
        'watch_item_promoted',
        project_path=project_path,
        item_id=item_id,
        kind=kind,
        charter_version=result.get('version'),
    )
    return {'ok': True, 'version': result.get('version')}

__all__ = [
    'add_watch_item', 'edit_watch_item', 'set_watch_status', 'delete_watch_item',
    'list_watch_items', 'generate_item_response', 'address_watch_item',
    'address_open_items', 'promote_watch_item', 'promotion_state',
    'answer_follow_up', 'render_goals_injection_block',
    'VALID_KINDS', 'VALID_STATUSES', 'PROMOTION_NONE', 'PROMOTION_ACTIVE',
    '_RESPONSES_KEEP',
]
