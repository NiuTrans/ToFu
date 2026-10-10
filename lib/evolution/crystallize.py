"""Macro crystallization: repeated winning tool sequences become proposals.

Responsibility: mine successful episodes' bound tasks for recurring tool-call
sequences and route them through the evolution gate as ``macro_tool``
mutations. Entry point: ``propose_macro_crystallizations``. Dependencies:
the episodes store, an injectable owner-scoped task loader, and the gate.

A sequence crystallizes only when the same ordered tool names appear in at
least two successful episodes — recurrence is the evidence, and the evidence
episode ids ride on the mutation. Existing proposed/active macros with the
same sequence are not re-proposed.
"""
from __future__ import annotations

from lib.log import get_logger

from .bridge import default_task_loader
from .episodes import list_episodes
from .gate import list_mutations, propose_mutation
from .contracts import EvolutionStateError

logger = get_logger(__name__)

MIN_SEQUENCE = 2
MAX_SEQUENCE = 16
MIN_OCCURRENCES = 2
MIN_FLOW_OCCURRENCES = 3


def _tool_sequence(task: dict) -> tuple[str, ...]:
    names = []
    for round_ in (task or {}).get('toolRounds') or []:
        if not isinstance(round_, dict):
            continue
        for call in round_.get('tool_calls') or []:
            if isinstance(call, dict) and call.get('name'):
                names.append(str(call['name']))
    return tuple(names[:MAX_SEQUENCE])


def _winning_sequences(load, *, user_id, episode_reader=None) -> dict[tuple[str, ...], list[str]]:
    """Mine successful episodes for recurring ordered tool sequences.

    One unloadable episode must not abort the whole mining pass.
    """
    sequences: dict[tuple[str, ...], list[str]] = {}
    for episode in (episode_reader or list_episodes)(user_id=user_id, limit=200):
        if episode.get('status') != 'success':
            continue
        task_id = str(episode.get('task_id') or '')
        if not task_id:
            continue
        try:
            task = load(task_id, user_id=user_id)
        except Exception as exc:
            logger.debug('[Evolution:Crystallize] episode task load failed: %s', exc)
            task = None
        sequence = _tool_sequence(task or {})
        if len(sequence) < MIN_SEQUENCE:
            continue
        sequences.setdefault(sequence, []).append(episode['episode_id'])
    return sequences


def _live_sequences(kind: str, *, user_id) -> set[tuple[str, ...]]:
    return {tuple((m.get('payload') or {}).get('sequence') or ())
            for m in list_mutations(user_id=user_id, kind=kind)
            if m.get('status') in ('proposed', 'confirmed', 'active')}


def propose_macro_crystallizations(*, user_id, loader=None,
                                   now: int | None = None, episode_reader=None) -> list[dict]:
    load = loader or default_task_loader
    sequences = _winning_sequences(load, user_id=user_id, episode_reader=episode_reader)
    existing = _live_sequences('macro_tool', user_id=user_id)

    proposals = []
    for sequence, episode_ids in sorted(sequences.items()):
        if len(episode_ids) < MIN_OCCURRENCES or sequence in existing:
            continue
        try:
            proposals.append(propose_mutation(
                'macro_tool',
                {'sequence': list(sequence),
                 'title': ' > '.join(sequence)[:200]},
                episode_ids[:32], user_id=user_id, now=now, episode_reader=episode_reader))
        except EvolutionStateError:
            continue
    return proposals


def propose_flow_template_crystallizations(*, user_id, loader=None,
                                           now: int | None = None, episode_reader=None) -> list[dict]:
    """Stronger recurrence (≥3 wins) also crystallizes the choreography:
    the sequence becomes a small validated worker→critic→deliverable
    definition routed through the gate as a ``flow_template`` mutation —
    activation materializes it into the owner's saved-flow catalogue."""
    load = loader or default_task_loader
    sequences = _winning_sequences(load, user_id=user_id, episode_reader=episode_reader)
    existing = _live_sequences('flow_template', user_id=user_id)

    from .flow_templates import build_flow_definition
    proposals = []
    for sequence, episode_ids in sorted(sequences.items()):
        if len(episode_ids) < MIN_FLOW_OCCURRENCES or sequence in existing:
            continue
        title = ' > '.join(sequence)[:200]
        try:
            proposals.append(propose_mutation(
                'flow_template',
                {'sequence': list(sequence), 'title': title,
                 'definition': build_flow_definition(sequence, title=title)},
                episode_ids[:32], user_id=user_id, now=now, episode_reader=episode_reader))
        except EvolutionStateError:
            continue
    return proposals
