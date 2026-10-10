"""Deterministic task-start presentation using frozen producer evidence only.

Entry point: show_task_start_time. This formatter returns the registered action
output; the owned event publisher handles persistence and visibility. No model,
wall clock, conversation creation, or ambient machine timezone is consulted.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contract import LIMITS
from .errors import AutomationError
from .registry import builtin_registry


def show_task_start_time(arguments: dict, event: dict) -> dict:
    contract = builtin_registry().get('task.show_time')
    contract.validate(arguments)
    if not isinstance(event, dict) or event.get('kind') != 'task.started':
        raise AutomationError('task_start_evidence_required', 'Time presentation requires an actual task-start event')
    evidence = event.get('evidence')
    started = evidence.get('startedAt') if isinstance(evidence, dict) else None
    if (type(started) not in {int, float} or not 0 < started <= LIMITS['safeIntegerMax']//1000
            or type(event.get('occurredAt')) not in {int, float} or started != event.get('occurredAt')):
        raise AutomationError('invalid_task_start_evidence', 'Task start time differs from frozen producer evidence')
    zone_name = arguments.get('timezone', 'UTC')
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise AutomationError('invalid_action_timezone', 'Choose an available IANA timezone') from exc
    try:
        local = datetime.fromtimestamp(started, timezone.utc).astimezone(zone)
    except (OverflowError, OSError, ValueError) as exc:
        raise AutomationError('invalid_task_start_evidence', 'Task start time cannot be displayed') from exc
    result = {'startedAt': started, 'timezone': zone_name,
              'text': f'任务开始时间：{local.isoformat(timespec="milliseconds")} [{zone_name}]'}
    contract.validate(result, output=True)
    return result
