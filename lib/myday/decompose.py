"""Decompose one owner goal into small suggested items.

Responsibility: ask the model for the small concrete items still missing from
one goal, given the items the owner already decided (kept, adopted or
declined) and the linked request's Workers, and parse its JSON answer. The
result is a list of proposals; ``GoalStore.record_decomposition`` merges it
idempotently. A failed call changes nothing.

The call uses the shared optional-analysis policy of this page (strict
billing-stop admission and the optional 429 allowance, as daily-report analysis
does) with dispatch_chat's defaults, so it adds no tuning parameter.

Entry points: build_messages, parse_items, propose_items, DecompositionError.
Dependencies: lib.llm_dispatch.dispatch_chat, lib.llm.llm_json.extract_json.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json

from lib.log import get_logger

logger = get_logger(__name__)

SYSTEM = """\
You help one person reach a goal they wrote for themselves. Break the goal into
the small, concrete items that are still missing.

Return ONLY a JSON object: {"items": ["item", "item", ...]}

Rules:
- Each item is one concrete action the person can check off when it is done,
  small enough to finish in one sitting. Start with a verb. Keep it under
  60 characters.
- Write every item in the language the goal is written in.
- Order items in the order they should be done.
- Never repeat, reword or split an item the person already has or declined.
- Workers listed under the goal already work on their part; do not duplicate
  their work, but you may add the person's own follow-ups (decide, review,
  verify).
- Do not invent facts about the person's projects or deadlines. If the goal is
  vague, start with an item that makes it concrete.
- Return {"items": []} when nothing is missing.
"""


class DecompositionError(RuntimeError):
    """The model gave no usable proposal list; the goal is unchanged."""


_STATUS_WORDS = {'pending': 'not started', 'in_progress': 'in progress', 'completed': 'done'}


def build_messages(goal: Mapping, workers: Iterable[Mapping] = ()) -> list[dict]:
    """The prompt: the goal, its decided items, declined suggestions and linked Workers."""
    kept, declined = [], []
    for item in goal['items']:
        if item['origin'] == 'agent' and item['proposal'] == 'dismissed':
            declined.append(item['text'])
        elif item['origin'] != 'evidence' and not (item['origin'] == 'agent' and item['proposal'] == 'pending'):
            kept.append(f"- {item['text']} ({_STATUS_WORDS[item['status']]})")
    sections = ['Goal:\n' + goal['text'],
                'Items the person already has:\n' + ('\n'.join(kept) or '(none)')]
    if declined:
        sections.append('Suggestions the person declined (never propose them again):\n'
                        + '\n'.join('- ' + text for text in declined))
    workers = list(workers)
    if workers:
        sections.append('Workers already assigned to this goal:\n' + '\n'.join(
            f"- {worker['title']} ({worker['state'].replace('_', ' ')})" for worker in workers))
    return [{'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': '\n\n'.join(sections)}]


def parse_items(content: str) -> list[str] | None:
    """Item texts from the model's JSON answer; None when it is not a proposal list."""
    from lib.llm.llm_json import extract_json
    data = extract_json(content or '', repair=True)
    if isinstance(data, dict):
        data = data.get('items')
    if not isinstance(data, list):
        return None
    texts = []
    for entry in data:
        if isinstance(entry, Mapping):
            entry = entry.get('text')
        if isinstance(entry, str) and entry.strip():
            texts.append(entry.strip())
    return texts


def _dispatch(messages: list[dict], owner: int) -> str:
    from lib.key_stats import strict_billing_stop_admission
    from lib.llm_dispatch.api import dispatch_chat
    from lib.production.llm_policy import optional_llm_max_429_attempts
    with strict_billing_stop_admission():
        content, usage = dispatch_chat(messages, capability='text', owner_user_id=owner,
                                       log_prefix='[MyDayGoals]',
                                       max_429_attempts=optional_llm_max_429_attempts())
    route = usage.get('_dispatch', {}) if isinstance(usage, dict) else {}
    logger.info('[MyDayGoals] decomposition via %s in %sms', route.get('model', '?'),
                route.get('latency_ms', '?'))
    return content


def propose_items(owner: int, goal: Mapping, *, workers: Iterable[Mapping] = (),
                  llm: Callable[[list[dict], int], str] | None = None) -> list[str]:
    """Ask the model once for the goal's missing items; raise DecompositionError on no usable answer."""
    messages = build_messages(goal, workers)
    try:
        content = (llm or _dispatch)(messages, owner)
    except DecompositionError:
        raise
    except Exception as error:
        raise DecompositionError(f'The model call failed: {error}') from error
    texts = parse_items(content)
    if texts is None:
        logger.warning('[MyDayGoals] unusable decomposition answer: %.300s', json.dumps(content)[:300])
        raise DecompositionError('The model did not return an item list')
    return texts


__all__ = ['DecompositionError', 'SYSTEM', 'build_messages', 'parse_items', 'propose_items']
