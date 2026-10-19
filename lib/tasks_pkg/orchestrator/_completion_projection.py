"""Final Request Inspector state projection shared by Python/native execution.

Entry publish_final_messages_snapshot. Existing sanitization and event owners
supply wire state; this leaf never appends history, closes rounds or persists
terminal state. Snapshot failure keeps the existing best-effort diagnostic path.
"""
from lib.log import get_logger

logger = get_logger(__name__)


def publish_final_messages_snapshot(task, messages, *, model, tid, tools, publish):
    try:
        from lib.tasks_pkg.manager import task_user_id
        from lib.tasks_pkg.wire_messages import apply_wire_sanitize
        from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
        from lib.agent_core.events import EventType, build_event
        wire = apply_wire_sanitize(messages, conv_id=task.get('convId', ''),
            provider_id=task.get('provider_id') or '', user_id=task_user_id(task))
        snapshot = _strip_base64_for_snapshot(wire)
        event = build_event(EventType.MESSAGES_SNAPSHOT, kind='state', model=model,
            roundNum='final', label=f'最终回复后 · {len(snapshot)}条', messages=snapshot,
            contextManifest=list(task.get('_contextManifest') or []))
        if tools:
            event['tools'] = tools
        publish(task, event)
    except Exception:
        logger.warning('[Task %s] final messages_snapshot failed model=%s',
                       tid, model, exc_info=True)
