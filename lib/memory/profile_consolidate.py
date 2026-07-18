"""lib/memory/profile_consolidate.py — the layer-3 consolidation pass.

After a conversation turn completes, scan the recent surface + the current
bounded profile with the CHEAP model (the same one wired in
``lib/memory/prefetch.py``) and produce candidate preference edits:

  * **reinforce** — an existing preference is restated / sharpened. AUTO-applied
    via ``apply_reinforcement`` (replace-in-place; never grows unbounded).
  * **new** — a genuinely new durable preference OR a durable fact about who the
    user is. AUTO-applied via ``apply_new_preference`` and surfaced to the user
    as a quiet "updated your profile" chip. The user is INFORMED, not asked: the
    profile is fully editable in Settings, so a wrong inference is cheaply
    corrected there rather than gated behind an in-chat confirmation.
  * **distil** — when the profile is over the hard cap, the model returns a
    rewritten, shorter ``full_profile`` that preserves meaning. AUTO-applied
    (it's a compression of what's already there, not a new fact), and it is the
    cap's forcing function: we replace the whole body rather than append-and-grow.

The pass is advisory and best-effort: any failure logs + returns an empty
result. It is gated on the Memory toggle + a feature flag, and skipped for
trivially short conversations.

Returns a list of ``learned`` dicts the orchestrator turns into
``preference_learned`` SSE events:
    {'kind': 'reinforced'|'added', 'summary': str, 'pending': False, 'id': ''}
"""

from __future__ import annotations

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
You maintain a SHORT, durable profile that helps an AI assistant be more \
thoughtful and personal toward ONE user. It holds two kinds of durable facts:
  1. WORKING PREFERENCES — how they like the assistant to work (language, tone, \
verbosity, coding conventions, do's/don'ts).
  2. ABOUT THE USER — stable facts about who they are that make the assistant \
more considerate (their role/profession, domain or industry, the languages & \
tech stack they work in, expertise level, recurring projects or goals, timezone/\
locale). NOT task facts, NOT one-off requests, NOT anything about the current \
repo/bug.

You are given the user's CURRENT profile and the RECENT conversation. Decide \
what — if anything — should change. Be CONSERVATIVE: most turns yield nothing. \
Only record something the user EXPLICITLY stated or clearly demonstrated — never \
guess or infer sensitive attributes.

Use the header "Preferences" for working preferences and "About the user" for \
identity facts.

Rules:
  - Only durable, general facts ("always reply in Chinese", "never add \
docstrings I didn't ask for", "is a backend engineer", "works mainly in Rust"). \
NOT "fix this bug", NOT facts about the current task/repo.
  - If the fact is ALREADY in the profile, do nothing (return []) unless the \
user sharpened/changed it → then a "reinforce" that REPLACES the old line.
  - A genuinely NEW preference or identity fact → kind "new" with the right \
"header". It is applied automatically and the user is told (they can edit it in \
Settings), so be accurate and conservative.
  - If told the profile is OVER its size cap, return ONE "distil" action whose \
"full_profile" is a rewritten, SHORTER profile preserving every fact's meaning \
(merge duplicates, drop stale, tighten wording). Markdown bullets under headers.

Return ONLY JSON:
  {"actions": [
     {"kind": "reinforce", "old_text": "<exact unique substring of current \
profile, usually the full bullet line>", "new_text": "- <updated bullet>"},
     {"kind": "new", "header": "Preferences"|"About the user", "text": "<the \
fact, no leading dash>", "evidence": "<≤1 sentence why>"},
     {"kind": "distil", "full_profile": "<entire rewritten profile markdown>"}
  ]}
Return {"actions": []} when nothing should change. Prefer fewer actions."""


def _recent_surface(messages: list, cap: int = _MAX_SURFACE_CHARS) -> str:
    """Plain-text of the last user+assistant turns (reuses prefetch helpers)."""
    from lib.memory.prefetch import _build_recent_turns_text
    txt = _build_recent_turns_text(messages, k=4)
    return txt[:cap]


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
    profile = up.load_profile(scope)
    over_cap = up.profile_over_cap(profile, scope)

    user_block = (
        f'## Current profile ({up.profile_char_count(profile)} chars, '
        f'cap {up.USER_PROFILE_CHAR_CAP}'
        f'{", OVER CAP — return a distil action" if over_cap else ""})\n\n'
        f'{profile or "(empty)"}\n\n'
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
            if kind == 'reinforce':
                res = up.apply_reinforcement(act.get('old_text', ''),
                                             act.get('new_text', ''),
                                             scope=scope)
                if res.get('matched') and res.get('saved'):
                    summary = (act.get('new_text') or '').lstrip('-*').strip()
                    learned.append({'kind': 'reinforced', 'summary': summary,
                                    'pending': False, 'id': ''})
                    audit_log('user_profile_learned', kind='reinforced')
            elif kind == 'distil':
                full = act.get('full_profile') or ''
                if full.strip():
                    res = up.save_profile(full, scope)
                    if res.get('saved'):
                        logger.info('[ProfileConsolidate] distilled profile '
                                    '→ %d chars (over_cap=%s)',
                                    res.get('chars'), res.get('over_cap'))
                        audit_log('user_profile_distilled',
                                  chars=res.get('chars'))
            elif kind == 'new':
                text = (act.get('text') or '').strip()
                if not text:
                    continue
                header = (act.get('header') or '').strip() or 'Preferences'
                res = up.apply_new_preference(text, header=f'## {header}',
                                              scope=scope)
                if res.get('saved'):
                    learned.append({'kind': 'added',
                                    'summary': text,
                                    'pending': False,
                                    'id': ''})
                    audit_log('user_profile_learned', kind='added')
        except Exception as e:
            logger.warning('[ProfileConsolidate] action %r failed: %s', kind, e)
            continue

    if learned:
        logger.info('[ProfileConsolidate] %d preference update(s): %s',
                    len(learned), [l['kind'] for l in learned])
    return learned
