"""lib/memory/profile_consolidate.py — the layer-3 consolidation pass.

After a conversation turn completes, scan real user text + the structured
"My Context" document with the configured CHEAP model.  It may conservatively
add or update one of three explicit types: identity facts, conditional work
rules, and response preferences.  Model output is accepted only when it cites
verbatim evidence from the real user surface and passes deterministic shape,
size, type, and de-duplication checks.  Every assistant change is recorded in
a bounded undo log and surfaced to the user with a real ``change_id``.

The pass is advisory and best-effort: any failure logs + returns an empty
result. It is gated on the independent My Context capability + a feature flag.
Short turns are reviewed only when they contain an explicit durable-context
signal, so a concise statement such as "I prefer short answers" is learnable
without sending acknowledgements and ordinary one-off requests to the model.

Returns a list of ``learned`` dicts the orchestrator turns into
``preference_learned`` SSE events:
    {'kind': 'reinforced'|'added', 'summary': str, 'pending': False,
     'id': str, 'change_id': str, 'item_id': str, 'type': str}
"""

from __future__ import annotations

import json
import re
import unicodedata

from lib.llm_json import extract_json
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = ['run_profile_consolidation', 'CONSOLIDATE_ENABLED']

# A short explicit fact can be more valuable than a long task description.
# Below the legacy 200-char cost gate, review only text with a likely durable
# context signal.  The model + grounded-output validator remain authoritative.
_MIN_SURFACE_CHARS = 4
_LONG_SURFACE_REVIEW_CHARS = 200
# How much recent text to feed the model.
_MAX_SURFACE_CHARS = 6000
# This advisory pass is retried naturally by future user turns.  It must yield
# after one real provider-capacity rejection instead of competing with the
# foreground task across the dispatcher's model rotation.
_MAX_429_ATTEMPTS = 1
# A model response is bounded before any profile write.  Two lets one explicit
# statement carry both an identity fact and a response preference without
# turning a single turn into an unreviewable profile rewrite.
_MAX_ACTIONS_PER_PASS = 2
_MAX_CONTEXT_FIELD_CHARS = 200
_MAX_EVIDENCE_CHARS = 400

_DURABLE_SIGNAL_PATTERNS = (
    re.compile(
        r'(?:我|本人)(?:是|在.{0,40}(?:工作|任职|就职|担任)|负责|从事|擅长|'
        r'专注(?:于)?|常用|主要使用)'),
    re.compile(
        r'(?:我的|本人(?:的)?)(?:偏好|习惯|工作习惯|工作规则|工作知识|回答偏好|回复偏好|'
        r'职业|职位|岗位|角色|公司|团队|组织|专业|专长|技术栈|工作环境)'),
    re.compile(
        r'(?:我|本人)(?:更?喜欢|更?偏好|习惯(?:于)?|不喜欢|不希望)'),
    re.compile(
        r'(?:我们|我司|本公司|团队).{0,80}(?:必须|总是|默认|一律|只能|'
        r'只用|统一|务必)'),
    re.compile(
        r'(?:请)?(?:记住|以后|今后|往后|从现在起|始终|一直|默认|每次|'
        r'每当|一律|务必|永远|不要再)'),
    re.compile(
        r"\b(?:i(?:'m| am)\s+(?:an?\s+)?(?:engineer|developer|designer|"
        r'manager|researcher|student|teacher|founder|consultant)|'
        r'i work\s+(?:at|for|as|in)\b|my\s+(?:preference|preferences|role|job|'
        r'company|team|organization|profession|expertise|stack|environment)\b|'
        r'i\s+(?:prefer|like|dislike|always|never)\b|please\s+(?:always|remember)\b|'
        r'from now on\b|going forward\b|by default\b|whenever\b)',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:always|never|remember\s+(?:this|that|it)|in\s+future\s+'
        r'conversations?|long[- ]term|durable\s+(?:fact|rule|preference|context))\b',
        re.IGNORECASE,
    ),
)

_LOW_VALUE_FIELD_PREFIX = re.compile(
    r'^(?:(?:the user|user)\s+(?:said|says|stated|mentioned|wants|prefers|'
    r'is|works)|(?:用户|该用户)(?:说|表示|提到|希望|想要|是)|'
    r'(?:对话中|本轮|当前任务)(?:用户)?(?:说|表示|提到|希望|要求))',
    re.IGNORECASE,
)
_LOW_VALUE_FIELDS = frozenset({
    'unknown', 'none', 'n/a', 'not specified', 'unspecified',
    '未知', '无', '未说明', '未提及',
})
_GROUNDING_STOPWORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'for', 'from', 'i', 'in',
    'is', 'it', 'me', 'my', 'of', 'on', 'or', 'that', 'the', 'this', 'to',
    'was', 'were', 'with',
})

try:
    from lib import _resolve_feature_flag  # type: ignore
    CONSOLIDATE_ENABLED = _resolve_feature_flag(
        'PROFILE_CONSOLIDATE', 'profile_consolidate', True)
except Exception as _e:  # pragma: no cover — defensive
    logger.warning('[ProfileConsolidate] feature-flag resolve failed: %s', _e)
    CONSOLIDATE_ENABLED = True


_SYSTEM_PROMPT = """\
You maintain a SHORT, durable "My Context" document for ONE user. It has
exactly three item types:
  1. identity — stable, explicitly stated facts about the user (role,
     organization, expertise, durable environment).
  2. work_rule — a reusable conditional rule with separate `condition` and
     `action` fields (for example: condition "submitting jobs on our cluster",
     action "use the hope MCP").
  3. response_preference — how the user wants answers written or work presented.

Be VERY CONSERVATIVE. Most turns yield no action. Only learn facts the USER
explicitly stated. Never learn from the assistant's own answer or reasoning.
Never infer sensitive attributes. Do not save a one-off request or output
format for just this turn, temporary state, current bug/repository fact, chat
summary, model reasoning, solution approach, generic lesson, compliment, or
social filler. Those are not user context.

Every action MUST include `evidence`: a short VERBATIM quote copied from a
recent [user] message. Paraphrased or invented evidence is invalid. The saved
fields must be the shortest standalone statement that preserves the useful
fact or rule. Do not write framing such as "the user said", explanations,
rationale, reminders, headings, bullets, or conversational filler. Emit at
most two actions, and prefer zero or one. Reuse the user's wording for the
saved fields; do not translate it or substitute distant synonyms.

If an item already exists, do nothing unless the user explicitly corrected or
sharpened it. Updates MUST reference the exact existing `item_id`. Do not
distil, merge, delete, or rewrite unrelated items.

Return ONLY JSON:
  {"actions": [
    {"kind":"new", "type":"identity"|"response_preference",
     "text":"...", "evidence":"short explicit user statement"},
    {"kind":"new", "type":"work_rule", "condition":"...",
     "action":"...", "evidence":"short explicit user statement"},
    {"kind":"update", "item_id":"ctx_...", "type":"...",
     "text":"...", "evidence":"verbatim user correction"},
    {"kind":"update", "item_id":"ctx_...", "type":"work_rule",
     "condition":"...", "action":"...",
     "evidence":"verbatim user correction"}
  ]}
Return {"actions":[]} when uncertain. Prefer zero or one action."""


def _recent_surface(messages: list, cap: int = _MAX_SURFACE_CHARS, *,
                    user_message_limit: int = 4) -> str:
    """Plain text from recent real USER messages only.

    Excluding assistant/tool/synthetic messages here is a data-boundary
    guarantee, not merely a prompt suggestion: the learner cannot turn its own
    reasoning, a tool result, or an injected reminder into user context.
    """
    from lib.memory.prefetch._query import _msg_plain_text

    # Walk newest-first and spend the fixed budget there. The old join-then-
    # slice shape kept the oldest of four messages, so one long historical
    # request could silently remove the just-finished user's explicit fact.
    rows_newest_first: list[str] = []
    used = 0
    for message in reversed(messages):
        if message.get('role') != 'user' or message.get('_isMeta'):
            continue
        text = _msg_plain_text(message).strip()
        if not text:
            continue
        row = f'[user] {text}'
        separator_chars = 2 if rows_newest_first else 0
        available = cap - used - separator_chars
        if available <= 0:
            break
        if len(row) > available:
            if rows_newest_first:
                break
            marker = '\n[... user message shortened ...]\n'
            if available <= len(marker) + 2:
                row = row[-available:]
            else:
                head_chars = (available - len(marker)) // 2
                tail_chars = available - len(marker) - head_chars
                row = row[:head_chars] + marker + row[-tail_chars:]
        rows_newest_first.append(row)
        used += separator_chars + len(row)
        if len(rows_newest_first) >= user_message_limit:
            break
    return '\n\n'.join(reversed(rows_newest_first))


def _parse_actions(content: str) -> list[dict]:
    """Tolerant JSON extraction of the actions list (fences/preamble safe)."""
    obj = extract_json(content)
    if not isinstance(obj, dict):
        return []
    acts = obj.get('actions')
    return acts if isinstance(acts, list) else []


def _comparison_text(value: object) -> str:
    """Unicode-stable text for evidence grounding and exact de-duplication."""
    normalized = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ' '.join(normalized.split())


def _surface_is_worth_reviewing(surface: str) -> bool:
    """Keep short high-signal facts while avoiding a cheap call for small talk."""
    plain = re.sub(r'(?m)^\[user\]\s*', '', surface).strip()
    if len(plain) < _MIN_SURFACE_CHARS:
        return False
    if len(plain) >= _LONG_SURFACE_REVIEW_CHARS:
        return True
    return any(pattern.search(plain) for pattern in _DURABLE_SIGNAL_PATTERNS)


def _clean_context_field(value: object) -> str:
    """Return one compact field or ``''`` when the model emitted low-value text."""
    raw = str(value or '').strip()
    if not raw or '\n' in raw or '\r' in raw:
        return ''
    compact = ' '.join(raw.split())
    if not (2 <= len(compact) <= _MAX_CONTEXT_FIELD_CHARS):
        return ''
    compared = _comparison_text(compact)
    if compared in _LOW_VALUE_FIELDS or _LOW_VALUE_FIELD_PREFIX.match(compact):
        return ''
    if compact.startswith(('-', '*', '•', '#')):
        return ''
    return compact


def _grounded_evidence(action: dict, surface: str) -> str:
    """Return exact user evidence, rejecting paraphrase and token fragments."""
    evidence = ' '.join(str(action.get('evidence') or '').split())
    if not evidence or len(evidence) > _MAX_EVIDENCE_CHARS:
        return ''
    normalized = _comparison_text(evidence)
    glyphs = [char for char in normalized if char.isalnum()]
    cjk_glyphs = [char for char in glyphs if ord(char) > 127]
    if len(glyphs) < 5 and len(cjk_glyphs) < 3:
        return ''
    return evidence if normalized in _comparison_text(surface) else ''


def _item_signature(item: dict) -> tuple[str, ...]:
    item_type = str(item.get('type') or '')
    if item_type == 'work_rule':
        return (
            item_type,
            _comparison_text(item.get('condition')),
            _comparison_text(item.get('action')),
        )
    return (item_type, _comparison_text(item.get('text')))


def _field_is_supported(field: str, evidence: str) -> bool:
    """Require saved meaning to reuse concrete words/chars from its evidence."""
    compared_field = _comparison_text(field)
    compared_evidence = _comparison_text(evidence)
    if compared_field in compared_evidence:
        return True

    field_cjk = {
        char for char in compared_field if char.isalnum() and ord(char) > 127
    }
    if field_cjk:
        evidence_cjk = {
            char for char in compared_evidence
            if char.isalnum() and ord(char) > 127
        }
        return (len(field_cjk & evidence_cjk) / len(field_cjk)) >= 0.5

    def latin_terms(value: str) -> set[str]:
        return {
            token for token in re.findall(r'[a-z0-9][a-z0-9_+.#-]*', value)
            if token not in _GROUNDING_STOPWORDS
        }

    field_terms = latin_terms(compared_field)
    if not field_terms:
        return False
    evidence_terms = latin_terms(compared_evidence)
    return (len(field_terms & evidence_terms) / len(field_terms)) >= 0.5


def _validated_actions(actions: list[dict], surface: str,
                       existing: list[dict]) -> list[dict]:
    """Fail closed on ungrounded, verbose, duplicate, or type-changing output."""
    existing_by_id = {
        str(item.get('id') or ''): item for item in existing if item.get('id')
    }
    seen_signatures = {_item_signature(item) for item in existing}
    accepted: list[dict] = []

    # Inspect only the declared bound. A model cannot hide a large mutation
    # after a prefix of invalid actions.
    for raw_action in actions[:_MAX_ACTIONS_PER_PASS]:
        if not isinstance(raw_action, dict):
            continue
        evidence = _grounded_evidence(raw_action, surface)
        if not evidence:
            continue
        kind = str(raw_action.get('kind') or '').strip().casefold()
        if kind not in {'new', 'update', 'reinforce'}:
            continue

        action = dict(raw_action)
        current: dict | None = None
        item_id = str(action.get('item_id') or '').strip()
        if kind in {'update', 'reinforce'}:
            if not item_id and kind == 'reinforce':
                old_text = _comparison_text(action.get('old_text'))
                matches = [
                    item for item in existing
                    if _comparison_text(item.get('text')) == old_text
                ]
                item_id = str(matches[0].get('id') or '') \
                    if len(matches) == 1 else ''
                if 'text' not in action:
                    action['text'] = action.get('new_text')
            current = existing_by_id.get(item_id)
            if current is None:
                continue

        item_type = str(action.get('type') or '').strip()
        if not item_type and current is not None:
            item_type = str(current.get('type') or '')
        if not item_type and kind == 'new':
            header = str(action.get('header') or '').strip().casefold()
            item_type = {
                'about the user': 'identity',
                'preferences': 'response_preference',
                'response preferences': 'response_preference',
            }.get(header, '')
        if item_type not in {'identity', 'work_rule', 'response_preference'}:
            continue
        if current is not None and current.get('type') != item_type:
            continue

        cleaned: dict = {
            'kind': 'update' if kind == 'reinforce' else kind,
            'type': item_type,
            'evidence': evidence,
        }
        if current is not None:
            cleaned['item_id'] = item_id
        if item_type == 'work_rule':
            condition = _clean_context_field(action.get('condition'))
            required_action = _clean_context_field(action.get('action'))
            if not condition or not required_action:
                continue
            if not (_field_is_supported(condition, evidence)
                    and _field_is_supported(required_action, evidence)):
                continue
            cleaned.update({'condition': condition, 'action': required_action})
        else:
            text = _clean_context_field(action.get('text'))
            if not text or not _field_is_supported(text, evidence):
                continue
            cleaned['text'] = text

        candidate = {**(current or {}), **cleaned, 'type': item_type}
        signature = _item_signature(candidate)
        if signature in seen_signatures:
            continue
        accepted.append(cleaned)
        seen_signatures.add(signature)
    return accepted


def run_profile_consolidation(messages: list, task: dict | None = None) -> list[dict]:
    """Run one consolidation pass. Returns a list of `learned` summaries.

    Best-effort: returns [] on any failure or when nothing changed.
    """
    if not CONSOLIDATE_ENABLED:
        return []
    import lib.memory.user_profile as up

    # Only the just-finished turn may trigger another paid review. Historical
    # long requests remain useful evidence/context once the latest user message
    # has a durable signal, but cannot make every later acknowledgement repeat
    # the same background call.
    latest_surface = _recent_surface(messages, user_message_limit=1)
    if not _surface_is_worth_reviewing(latest_surface):
        return []
    surface = _recent_surface(messages)

    # Identity scope captured onto the task at creation (the daemon thread has
    # no request context). '' → the single global profile (open/private mode).
    scope = (task or {}).get('_profileScope', '') or ''
    context = up.context_status(scope)

    user_block = (
        f'## Current context ({context["chars"]} chars, '
        f'cap {context["cap"]})\n\n'
        f'{json.dumps(context["items"], ensure_ascii=False)}\n\n'
        f'## Recent conversation\n\n{surface}'
    )

    try:
        from lib.key_stats import strict_billing_stop_admission
        from lib.llm_dispatch import (
            DispatchSharedContentionDeferred,
            dispatch_chat,
        )
    except Exception as e:
        logger.warning('[ProfileConsolidate] LLM dispatch load failed: %s', e)
        return []

    try:
        with strict_billing_stop_admission():
            content, _usage = dispatch_chat(
                [{'role': 'system', 'content': _SYSTEM_PROMPT},
                 {'role': 'user', 'content': user_block}],
                max_tokens=2048, temperature=0, capability='cheap',
                log_prefix='[ProfileConsolidate]',
                max_429_attempts=_MAX_429_ATTEMPTS,
                defer_on_shared_contention=True,
            )
    except DispatchSharedContentionDeferred as e:
        logger.info(
            '[ProfileConsolidate] deferred by shared contention for %.1fs',
            e.retry_after_s,
        )
        return []
    except Exception as e:
        logger.warning('[ProfileConsolidate] cheap-LLM call failed: %s', e)
        return []

    actions = _validated_actions(
        _parse_actions(content or ''), surface, context['items'])
    if not actions:
        return []

    learned: list[dict] = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        kind = (act.get('kind') or '').strip()
        try:
            if kind == 'update':
                item_id = (act.get('item_id') or '').strip()
                if not item_id:
                    continue
                updates = {k: act[k] for k in
                           ('type', 'text', 'condition', 'action') if k in act}
                res = up.update_context_item(
                    item_id, updates, scope, source='assistant',
                    record_change=True)
                if res and res.get('saved'):
                    item = res['item']
                    summary = (item.get('text') or
                               f'When {item.get("condition")} → {item.get("action")}')
                    learned.append({
                        'kind': 'reinforced', 'summary': summary,
                        'pending': False, 'id': res['change_id'],
                        'change_id': res['change_id'], 'item_id': item['id'],
                        'type': item['type'],
                    })
                    audit_log('user_context_learned', kind='updated')
            elif kind == 'new':
                item_type = (act.get('type') or '').strip()
                if not item_type:
                    header = (act.get('header') or '').strip().casefold()
                    item_type = ('identity' if header == 'about the user'
                                 else 'response_preference')
                raw = {'type': item_type}
                for key in ('text', 'condition', 'action'):
                    if key in act:
                        raw[key] = act[key]
                existing = up.load_context(scope)['items']
                if any(all(item.get(k) == raw.get(k) for k in raw)
                       for item in existing):
                    continue
                res = up.create_context_item(
                    raw, scope, source='assistant', record_change=True)
                if res.get('saved'):
                    item = res['item']
                    summary = (item.get('text') or
                               f'When {item.get("condition")} → {item.get("action")}')
                    learned.append({
                        'kind': 'added', 'summary': summary,
                        'pending': False, 'id': res['change_id'],
                        'change_id': res['change_id'], 'item_id': item['id'],
                        'type': item['type'],
                    })
                    audit_log('user_context_learned', kind='added')
        except Exception as e:
            logger.warning('[ProfileConsolidate] action %r failed: %s', kind, e)
            continue

    if learned:
        logger.info('[ProfileConsolidate] %d preference update(s): %s',
                    len(learned), [l['kind'] for l in learned])
    return learned
