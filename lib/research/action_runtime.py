"""Bounded task runtime for evidence-producing research actions."""

from __future__ import annotations

from lib.production.runtime import ProductionRuntime


_production = ProductionRuntime(
    'research-action', id_prefix='research_action', ttl=7200,
    push_channel='research', error_source='lib.research.action',
    log_label='ResearchAction', max_events=1_000,
)
_research_action_runtime = _production.runtime


def create_action_task(task_id: str, *, user_id: int, fields: dict):
    return _production.create_task(
        task_id, user_id=user_id,
        meta={'direction': fields['direction'], 'action': fields['action'],
              'lang': fields['lang']},
        fields=fields,
    )


def append_action_event(task: dict, event: dict):
    return _production.append_event(task, event)


__all__ = [
    '_production', '_research_action_runtime', 'append_action_event',
    'create_action_task',
]
