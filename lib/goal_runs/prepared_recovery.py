"""Join reserved invocation identities to atomic native consumption witnesses.

Entry: join_consumed_preparations. This read preserves preparation as preparation.
Only the later native claim checks the protected retired-header witness and may
retire actors; an ordinary appended consumption event never grants a capability.
"""
from copy import deepcopy
from functools import lru_cache

from lib.orchestration.execution_journal import JournalRecovery, RecoveryRefused
from lib.orchestration.invocation_recovery import completion_binding


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    from tofu_db.generated_checkpoint_control import GOAL_CONSUMPTION_RECEIPT_SCHEMA
    return Draft202012Validator(GOAL_CONSUMPTION_RECEIPT_SCHEMA)


def join_consumed_preparations(events, journal):
    preparations = {}; witnesses = {}; started = set()
    try:
        for event in events:
            event = {key: value for key, value in event.items() if key != 'seq'}
            kind = event.get('type')
            if kind == 'execution_effect_prepared' and 'invocation_completion' in event:
                preparations[event['invocation_id']] = event
            elif kind in {'execution_effect_started', 'execution_effect_completed'}:
                started.add(event.get('invocation_id'))
            elif kind == 'execution_recovery_consumed':
                _validator().validate(event)
                actors = event['actors']
                rich = [actor for actor in actors if 'preparation' in actor]
                if not rich:
                    continue  # Legacy receipt has no pre-admission recovery proof.
                if len(rich) != len(actors) or len({row['source_task_id'] for row in actors}) != len(actors):
                    raise ValueError('partial or repeated consumed membership')
                for actor in actors:
                    task = actor['target_task_id']; prepared = actor['preparation']
                    if (task in witnesses or task in started or preparations.get(task) != prepared
                            or prepared['invocation_id'] != task or completion_binding(prepared) is None):
                        raise ValueError('consumed actor differs from its reserved preparation')
                    witnesses[task] = deepcopy(actor)
        pending = deepcopy(journal.pending_agents)
        for key, prepared in journal.prepared_agents.items():
            if prepared['invocation_id'] in witnesses:
                pending[key] = deepcopy(prepared)
        return JournalRecovery(journal.records, pending, journal.parent_run_id,
                               journal.prepared_agents), witnesses
    except RecoveryRefused:
        raise
    except Exception as exc:
        raise RecoveryRefused('Goal consumed preparation evidence is invalid') from exc
