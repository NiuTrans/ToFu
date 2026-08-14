"""lib/memory/profile_consolidate.py — the layer-3 consolidation pass.

After a conversation turn completes, scan the recent surface + structured
"My Context" document with the configured CHEAP model.  It may conservatively
add or update one of three explicit types: identity facts, conditional work
rules, and response preferences.  Every assistant change is recorded in a
bounded undo log and surfaced to the user with a real ``change_id``.

The pass is advisory and best-effort: any failure logs + returns an empty
result. It is gated on the Memory toggle + a feature flag, and skipped for
trivially short conversations.

Returns a list of ``learned`` dicts the orchestrator turns into
``preference_learned`` SSE events:
    {'kind': 'reinforced'|'added', 'summary': str, 'pending': False,
     'id': str, 'change_id': str, 'item_id': str, 'type': str}
"""

from __future__ import annotations

import json

from lib.llm_json import extract_json
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = ['run_profile_consolidation', 'CONSOLIDATE_ENABLED']

# Minimum conversational surface (chars) before we bother the cheap model.
_MIN_SURFACE_CHARS = 200
# How much recent text to feed the model.
_MAX_SURFACE_CHARS = 6000

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
Never infer sensitive attributes. Do not save a one-off request, temporary
state, current bug/repository fact, chat summary, model reasoning, solution
approach, or generic lesson. Those are not user context.

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
     "text":"..."},
    {"kind":"update", "item_id":"ctx_...", "type":"work_rule",
     "condition":"...", "action":"..."}
  ]}
Return {"actions":[]} when uncertain. Prefer zero or one action."""


def _recent_surface(messages: list, cap: int = _MAX_SURFACE_CHARS) -> str:
    """Plain text from recent real USER messages only.

    Excluding assistant/tool/synthetic messages here is a data-boundary
    guarantee, not merely a prompt suggestion: the learner cannot turn its own
    reasoning, a tool result, or an injected reminder into user context.
    """
    from lib.memory.prefetch._query import _msg_plain_text

    rows: list[str] = []
    for message in reversed(messages):
        if message.get('role') != 'user' or message.get('_isMeta'):
            continue
        text = _msg_plain_text(message).strip()
        if not text:
            continue
        rows.append(f'[user] {text}')
        if len(rows) >= 4:
            break
    rows.reverse()
    surface = '\n\n'.join(rows)
    return surface[:cap]


def _parse_actions(content: str) -> list[dict]:
    """Tolerant JSON extraction of the actions list (fences/preamble safe)."""
    obj = extract_json(content)
    if not isinstance(obj, dict):
        return []
    acts = obj.get('actions')
    return acts if isinstance(acts, list) else []


def run_profile_consolidation(messages: list, task: dict | None = None) -> list[dict]:
    """Run one consolidation pass. Returns a list of `learned` summaries.

    Best-effort: returns [] on any failure or when nothing changed.
    """
    if not CONSOLIDATE_ENABLED:
        return []
    from lib.memory import user_profile as up

    surface = _recent_surface(messages)
    if len(surface) < _MIN_SURFACE_CHARS:
        return []

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
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [{'role': 'system', 'content': _SYSTEM_PROMPT},
             {'role': 'user', 'content': user_block}],
            max_tokens=2048, temperature=0, capability='cheap',
            log_prefix='[ProfileConsolidate]',
        )
    except Exception as e:
        logger.warning('[ProfileConsolidate] cheap-LLM call failed: %s', e)
        return []

    actions = _parse_actions(content or '')
    if not actions:
        return []

    learned: list[dict] = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        kind = (act.get('kind') or '').strip()
        try:
            if kind in ('update', 'reinforce'):
                item_id = (act.get('item_id') or '').strip()
                # Back-compat for an older consolidator response shape.
                if not item_id and kind == 'reinforce':
                    old = (act.get('old_text') or '').lstrip('-*').strip()
                    matches = [i for i in up.load_context(scope)['items']
                               if i.get('text') == old]
                    item_id = matches[0]['id'] if len(matches) == 1 else ''
                    act = {**act, 'text': (act.get('new_text') or '')
                           .lstrip('-*').strip()}
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
