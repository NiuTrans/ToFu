"""lib/optimizer/proposer.py — Single-shot LLM proposal generation.

Uses the cheap dispatch tier (``smart_chat`` with ``capability='cheap'``)
to turn an :class:`EvidenceBundle` into a list of structured proposals.
The LLM returns JSON; we validate + sanitise server-side (never trust
the model to invent action types).
"""

from __future__ import annotations

import json
import re

from lib.log import get_logger

from .actions import ACTION_REGISTRY
from .analyzer import EvidenceBundle

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Prompt construction
# ══════════════════════════════════════════════════════════

_SYSTEM = (
    'You are the Tofu Daily Optimizer. You analyse evidence from the '
    'last 24 hours of server activity and propose small, low-risk '
    'improvements. You return STRICT JSON only, no prose, no markdown '
    'fences. Every action_type MUST come from the whitelist provided.'
)


# Kept explicit (not derived from ACTION_REGISTRY) so the prompt is
# deterministic and reviewers see exactly what the LLM may propose.
# Any action_type the LLM returns outside this list is coerced to
# "other" by ``_validate_proposal``.
_ALLOWED_ACTION_TYPES = [
    'block_search_domain',        # auto-apply
    'adjust_fetch_timeout',        # suggest-only
    'adjust_fetch_top_n',          # suggest-only
    'toggle_llm_content_filter',   # suggest-only
    'suggest_model_fallback',      # suggest-only
    'extend_cache_ttl',            # suggest-only
    'disable_failing_scheduled_task',   # suggest-only
    'relax_proactive_poll_schedule',    # suggest-only
    'tighten_compaction_threshold',     # suggest-only
    'promote_memory_note',         # suggest-only
    'raise_rate_limit_cooldown',   # suggest-only
    'flag_expensive_model_pair',   # suggest-only
    'other',
]


_API_KEY_RE = re.compile(r'(sk-[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{32,})')


def _redact(text: str, limit: int = 500) -> str:
    """Truncate + redact api-key-shaped substrings."""
    if not text:
        return ''
    s = str(text)[:limit]
    return _API_KEY_RE.sub('[REDACTED]', s)


def _serialise_prior_actions(prior: list[dict]) -> str:
    if not prior:
        return '(none)'
    lines: list[str] = []
    for p in prior[:15]:
        args = p.get('args') or {}
        outcome = p.get('outcome_metric') or {}
        lines.append(
            f'- id={p.get("id")} action={p.get("action_type")} '
            f'args={json.dumps(args, ensure_ascii=False, default=str)[:200]} '
            f'status={p.get("proposal_status")} '
            f'applied={p.get("applied_at")} expires={p.get("expires_at")} '
            f'reverted={p.get("reverted_at") or "no"} '
            f'outcome={json.dumps(outcome, ensure_ascii=False, default=str)[:240]}'
        )
    return '\n'.join(lines)


def _build_user_prompt(evidence: EvidenceBundle) -> str:
    whitelist_lines = []
    for name in _ALLOWED_ACTION_TYPES:
        entry = ACTION_REGISTRY.get(name)
        auto = bool(entry and entry.get('auto_apply'))
        desc = (entry or {}).get('description') or '(pending_review only)'
        whitelist_lines.append(f'- {name} [auto_apply={auto}]: {desc}')
    whitelist = '\n'.join(whitelist_lines)

    prior = _serialise_prior_actions(evidence.prior_actions)

    search_domains = json.dumps(evidence.top_search_domains[:10],
                                ensure_ascii=False, default=str)[:1200]
    dropped = json.dumps(evidence.irrelevant_dropped_domains[:10],
                         ensure_ascii=False, default=str)[:1200]
    tool_calls = json.dumps(dict(list(evidence.tool_call_counts.items())[:20]),
                            ensure_ascii=False, default=str)[:1200]
    tool_errors = json.dumps(dict(list(evidence.tool_error_counts.items())[:20]),
                             ensure_ascii=False, default=str)[:1200]
    audit_events = json.dumps(evidence.audit_event_counts,
                              ensure_ascii=False, default=str)[:600]

    warn_lines = '\n'.join(_redact(l) for l in evidence.warn_log_excerpts[:30])
    error_lines = '\n'.join(_redact(l) for l in evidence.error_log_excerpts[:30])

    report_snips: list[str] = []
    for r in (evidence.daily_report_snippets or [])[:5]:
        report_snips.append(f'- {r.get("date")}: {_redact(r.get("summary") or "", 400)}')
    reports_block = '\n'.join(report_snips) if report_snips else '(none)'

    model_switches = json.dumps(evidence.model_switch_events[:8],
                                ensure_ascii=False, default=str)[:1200]
    failing_tasks = json.dumps(evidence.failing_scheduled_tasks[:8],
                               ensure_ascii=False, default=str)[:1000]
    idle_agents = json.dumps(evidence.idle_proactive_tasks[:8],
                             ensure_ascii=False, default=str)[:1000]
    cost_outliers = json.dumps(evidence.top_cost_conversations[:8],
                               ensure_ascii=False, default=str)[:800]

    return f'''## Evidence window
{evidence.window_hours}h ending {evidence.generated_at}

## Tool call counts (top 20)
{tool_calls}

## Tool error counts
{tool_errors}

## Top search-result domains (last 24h)
{search_domains}

## IRRELEVANT-dropped domains (content_filter)
{dropped}

## Reliability / performance counters
fetch_timeout={evidence.fetch_timeout_count} \
fetch_failure={evidence.fetch_failure_count} \
rate_limit_429={evidence.rate_limit_429_count} \
prompt_too_long={evidence.prompt_too_long_count} \
context_near_full={evidence.context_near_full_count} \
compaction_triggers={evidence.compaction_trigger_count}

## Recent model-switch audit events
{model_switches}

## Failing scheduled tasks (run_count≥5, fail_ratio≥0.5)
{failing_tasks}

## Idle proactive agents (poll_count≥20, execution_count=0)
{idle_agents}

## Top-cost conversations (latest cached day)
{cost_outliers}

## Audit event counts
{audit_events}

## Recent daily report summaries (last 7 days)
{reports_block}

## Recent WARNING excerpts (redacted, truncated)
{warn_lines or '(none)'}

## Recent ERROR excerpts (redacted, truncated)
{error_lines or '(none)'}

## Prior actions & their effect (learn from these)
{prior}

## Whitelisted action types
{whitelist}

## Task
Return STRICT JSON with this schema — no prose, no code fences:
{{
  "proposals": [
    {{
      "title": "short imperative",
      "rationale": "cite evidence numerically; why this helps",
      "action_type": "one of the whitelist",
      "action_args": {{ "...": "..." }},
      "severity": "low|med|high",
      "confidence": 0.0-1.0,
      "evidence_ids": ["short_ref_a", "short_ref_b"],
      "ttl_days": 7
    }}
  ]
}}

Rules:
* Only propose actions whose action_type is in the whitelist. Use
  "other" for anything that does not fit.
* For block_search_domain, action_args = {{"domain": "<lower-case host>", "ttl_days": <int 1..30>}}.
* If a prior action's outcome_metric shows the underlying problem has
  vanished (e.g. zero IRRELEVANT drops), DO NOT re-propose the same
  action; optionally propose revert by returning action_type="other"
  with rationale explaining "revert <id>".
* Be conservative. 0-3 proposals is normal. Empty list is fine.
'''


# ══════════════════════════════════════════════════════════
#  LLM call + JSON parsing
# ══════════════════════════════════════════════════════════

def _strip_fences(text: str) -> str:
    s = (text or '').strip()
    if s.startswith('```'):
        # Remove opening fence line
        s = s.split('\n', 1)[-1] if '\n' in s else s[3:]
        if s.endswith('```'):
            s = s[:-3]
    return s.strip()


def _validate_proposal(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get('title') or '').strip()
    rationale = str(raw.get('rationale') or '').strip()
    action_type = str(raw.get('action_type') or '').strip()
    if not (title and rationale and action_type):
        return None
    if action_type not in _ALLOWED_ACTION_TYPES:
        logger.warning('[Optimizer.proposer] LLM returned non-whitelist '
                       'action_type=%s — coercing to "other"', action_type)
        action_type = 'other'

    args = raw.get('action_args') or {}
    if not isinstance(args, dict):
        args = {}

    severity = str(raw.get('severity') or 'low').lower()
    if severity not in ('low', 'med', 'high'):
        severity = 'low'

    try:
        confidence = float(raw.get('confidence') or 0.5)
    except (TypeError, ValueError) as e:
        logger.debug('[Optimizer.proposer] bad confidence, defaulting: %s', e)
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    try:
        ttl_days = int(raw.get('ttl_days') or args.get('ttl_days') or 7)
    except (TypeError, ValueError) as e:
        logger.debug('[Optimizer.proposer] bad ttl_days, defaulting to 7: %s', e)
        ttl_days = 7
    ttl_days = max(1, min(30, ttl_days))
    args.setdefault('ttl_days', ttl_days)

    evidence_ids = raw.get('evidence_ids') or []
    if not isinstance(evidence_ids, list):
        evidence_ids = []

    return {
        'title': title[:500],
        'rationale': rationale[:4000],
        'action_type': action_type,
        'action_args': args,
        'severity': severity,
        'confidence': confidence,
        'ttl_days': ttl_days,
        'evidence_ids': [str(x)[:80] for x in evidence_ids][:20],
    }


def propose(evidence: EvidenceBundle,
             *, llm_override=None) -> list[dict]:
    """Ask the cheap LLM for a list of proposals.

    Args:
        evidence: EvidenceBundle from analyzer.gather_evidence().
        llm_override: optional callable(messages)->(content, usage). Used
            by the smoke test to inject a canned response.

    Returns:
        List of validated proposal dicts (may be empty).
    """
    user_prompt = _build_user_prompt(evidence)
    messages = [
        {'role': 'system', 'content': _SYSTEM},
        {'role': 'user', 'content': user_prompt},
    ]

    try:
        if llm_override is not None:
            content, usage = llm_override(messages)
        else:
            from lib.llm_dispatch import smart_chat
            content, usage = smart_chat(
                messages=messages,
                max_tokens=2048,
                temperature=0,
                capability='cheap',
                log_prefix='[Optimizer]',
            )
    except Exception as e:
        logger.error('[Optimizer.proposer] LLM call failed: %s', e, exc_info=True)
        return []

    logger.info('[Optimizer.proposer] LLM returned %d chars, usage=%s',
                len(content or ''), str(usage)[:200])

    text = _strip_fences(content or '')
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Optimizer.proposer] invalid JSON from LLM (len=%d): %s; '
                       'preview=%.200s', len(text), e, text)
        return []

    raw_list = data.get('proposals') if isinstance(data, dict) else None
    if not isinstance(raw_list, list):
        logger.warning('[Optimizer.proposer] LLM JSON missing "proposals" list; '
                       'preview=%.200s', text)
        return []

    out: list[dict] = []
    for item in raw_list[:20]:  # hard cap
        validated = _validate_proposal(item)
        if validated:
            out.append(validated)
        else:
            logger.warning('[Optimizer.proposer] rejected malformed proposal: %.200s',
                           json.dumps(item, ensure_ascii=False, default=str))
    logger.info('[Optimizer.proposer] produced %d valid proposals', len(out))
    return out
