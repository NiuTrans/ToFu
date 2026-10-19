"""One-shot LLM draft of a frozen experiment protocol for one accepted idea.

The auto-research pipeline already paid for the evidence: a literature
survey, an open-gap map, and accepted ideas with mechanism-level novelty
arguments.  When the owner promotes one idea into the production workspace,
this module turns THAT evidence into a complete protocol draft (hypothesis,
metric, baseline, dataset, falsifier, evaluation, stops) with a single
bounded, tool-less model call — the owner reviews and edits a grounded draft
instead of filling a blank form from memory.

The draft is a proposal, never persisted here: authority stays with the
versioned workspace PUT, which re-normalizes every field anyway.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lib.identity import require_user_id
from lib.llm.llm_json import extract_json
from lib.log import audit_log, get_logger

from .persistence import load_research_artifacts
from .program import normalize_program_fields
from .observation_audit import decision_rule_contract

logger = get_logger(__name__)

#: Single structured synthesis; the whole protocol fits one small reply.
_DRAFT_MAX_TOKENS = 4000
#: Drafting is a judgement on fixed evidence, not creative — deterministic.
_DRAFT_TEMPERATURE = 0.0
#: The survey is grounding context, not the payload; bound its excerpt.
_MAX_SURVEY_EXCERPT_CHARS = 12_000
#: Per-field prompt bound so one pathological idea cannot flood the call.
_MAX_IDEA_FIELD_CHARS = 2_000


def dispatch_stream(*args, **kwargs):
    """Facade → real dispatcher. Patched by tests as ``draft.dispatch_stream``."""
    from lib.llm_dispatch import dispatch_stream as _ds
    return _ds(*args, **kwargs)


def _find_idea(accepted: Any, idea_id: str) -> dict[str, Any] | None:
    """Match by stored id, falling back to the frontend's positional id."""
    for index, row in enumerate(list(accepted or [])):
        if not isinstance(row, Mapping):
            continue
        candidate = str(row.get('id') or f'idea-{index + 1}')
        if candidate == idea_id:
            return dict(row)
    return None


def _find_gap(open_gaps: Any, gap_id: str) -> dict[str, Any]:
    if not gap_id:
        return {}
    rows = open_gaps.get('open_gaps') if isinstance(open_gaps, Mapping) else None
    for row in list(rows or []):
        if isinstance(row, Mapping) and str(row.get('id') or '') == gap_id:
            return dict(row)
    return {}


def _idea_packet(idea: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: str(idea.get(key) or '')[:_MAX_IDEA_FIELD_CHARS]
        for key in (
            'title', 'kind', 'corpus_anchor_id', 'corpus_delta',
            'failure_cause', 'new_invariant', 'intervention_level',
            'core_mechanism', 'novelty_claim', 'falsifiable_prediction',
            'why_not_AB',
        )
        if str(idea.get(key) or '').strip()
    }


def _messages(direction: str, lang: str, idea: Mapping[str, Any],
              gap: Mapping[str, Any], survey_md: str) -> list[dict[str, str]]:
    language = 'Chinese' if lang == 'zh' else 'English'
    system = (
        'You are Tofu Research Foundry. Turn one accepted research idea into '
        'the smallest decisive experiment protocol a careful reviewer would '
        'accept. Ground every field in the provided survey, gap and idea — '
        'never invent a paper, dataset, baseline or metric the evidence does '
        'not support; when the surveyed literature establishes a standard '
        'choice, name it. The falsifier is the concrete observed result that '
        f'would kill the hypothesis. Write all prose in {language}. '
        'Return ONLY one JSON object, without markdown fences: '
        '{"hypothesis":str,"primary_metric":str,"baseline":str,"dataset":str,'
        '"falsifier":str,"evaluation_protocol":str,"environment":str,'
        '"random_seeds":[int],"stop_conditions":[str],"resources":str}. '
        'Propose decision_rule only when metric direction, minimum oriented effect and positive SE multiplier are grounded and explicit. '
        'Use the supplied scientific_decision_rule_contract. Omit the rule when those choices are unknown; do not guess defaults from a metric name. '
        'The rule is a proposal for review and freezing before execution, not a measured verdict or formal confidence interval. '
        'random_seeds: 2-5 distinct integers. stop_conditions: at most 6 short lines. '
        'resources: compute, wall-time and data budget in one sentence. '
        'Also return matched_budget, exploratory_plan, validation_plan strings. '

        'Return competing_hypotheses [{id,statement,prediction,scope}] and '
        'discriminating_experiments [{id,hypothesis_ids,design,decision_rule,'
        'expected_information,expected_value,cost}] in priority order. Costs and '
        'expected information/value are qualitative estimates, not invented probabilities. '
        'Design the minimum discriminating experiment: a strong baseline and '
        'explicit falsifier with matched total budgets including detector, tool, '
        'token, compute, wall-time and data costs. Separate exploratory tuning '
        'from held-out validation, freeze the decision threshold before validation, '
        'and specify supported/refuted/inconclusive decision rules with uncertainty. '
        'Do not launch jobs or imply that execution completion supports a hypothesis.')
    payload = {
        'scientific_decision_rule_contract': decision_rule_contract(),
        'direction': direction,
        'language': language,
        'idea': _idea_packet(idea),
        'linked_gap': dict(gap),
        'survey_excerpt': survey_md[:_MAX_SURVEY_EXCERPT_CHARS],
    }
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': json.dumps(
            payload, ensure_ascii=False, separators=(',', ':'), default=str)},
    ]


def draft_protocol(
    direction: str,
    lang: str,
    idea_id: str,
    *,
    user_id: int,
    model: str = '',
    source_task_id: str | None = None,
) -> dict[str, Any]:
    """Draft one frozen protocol for one accepted idea of one direction.

    Returns ``{'idea_id': str, 'hypothesis': str, 'protocol': dict}`` with the
    protocol normalized through the same program contract as a workspace PUT.
    Raises ``ValueError`` for an unknown direction/idea or an unusable draft.
    """
    user_id = require_user_id(user_id, context='research protocol draft owner')
    clean_direction = str(direction or '').strip()[:2_000]
    clean_idea_id = str(idea_id or '').strip()[:160]
    if not clean_direction or not clean_idea_id:
        raise ValueError('direction and idea_id are required')
    norm_lang = 'zh' if lang == 'zh' else 'en'
    if source_task_id is None:
        artifacts = load_research_artifacts(clean_direction, norm_lang, user_id=user_id)
    else:
        if not isinstance(source_task_id, str) or not source_task_id:
            raise ValueError('source_task_id must identify a saved research task')
        from .records import load_idea_source
        artifacts = load_idea_source(source_task_id, user_id=user_id,
            direction=clean_direction, lang=norm_lang, idea_id=clean_idea_id)
    if not artifacts.get('found'):
        raise ValueError('no persisted research artifacts for this direction')
    idea = _find_idea(artifacts.get('accepted'), clean_idea_id)
    if idea is None:
        raise ValueError('unknown idea_id for this direction')
    gap = _find_gap(artifacts.get('open_gaps'), str(idea.get('linked_gap_id') or ''))

    from lib.llm.stream_result import require_verified_provider_stream_result
    from lib.paper.agent_loop_policy import PAPER_AGENT_ROUTE_MAX_RETRIES

    buf = {'content': ''}

    def _on_content(chunk):
        buf['content'] += chunk

    stream_result = require_verified_provider_stream_result(
        dispatch_stream(
            _messages(clean_direction, norm_lang, idea, gap,
                      str(artifacts.get('survey_md') or '')),
            on_content=_on_content,
            prefer_model=model or None, strict_model=bool(model),
            capability='text', user_id=user_id,
            max_tokens=_DRAFT_MAX_TOKENS, temperature=_DRAFT_TEMPERATURE,
            thinking_enabled=False, max_retries=PAPER_AGENT_ROUTE_MAX_RETRIES,
            log_prefix='[Research:Draft]'),
        context='research protocol draft')
    message = stream_result.message
    content = buf['content'] or (
        str(message.get('content') or '') if isinstance(message, Mapping) else '')
    parsed = extract_json(content, repair=True)
    if not isinstance(parsed, Mapping):
        raise ValueError('protocol draft returned no valid JSON object')

    protocol = normalize_program_fields({'protocol': parsed})['protocol']
    hypothesis = str(parsed.get('hypothesis') or '').strip()[:12_000]
    if not hypothesis:
        hypothesis = str(
            idea.get('falsifiable_prediction')
            or idea.get('novelty_claim') or '')[:12_000]
    if not hypothesis and not any(protocol.values()):
        raise ValueError('protocol draft returned an empty protocol')

    audit_log('research_protocol_draft', direction=clean_direction[:120],
              idea_id=clean_idea_id, user_id=int(user_id))
    return {**({'source_task_id': source_task_id} if source_task_id is not None else {}),
            'idea_id': clean_idea_id, 'hypothesis': hypothesis,
            'protocol': protocol}


__all__ = ['draft_protocol']
