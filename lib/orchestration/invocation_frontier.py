"""Read paid native actor evidence for aggregate Goal recovery preflight.

Entry: read_registered_actor_frontier. The selected native journal proves a dead
writer and an unchanged version. Registered birth binds the actor to its source
launch. This read creates no lease, account, successor or replay permission.
"""
from copy import deepcopy
from functools import lru_cache
import time

from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.invocation_recovery import completion_binding, _record
from lib.tasks_pkg.invocation_registration import INVOCATION_COMPLETION_FIELD
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES


@lru_cache(maxsize=1)
def _validators():
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import SHARED_ACCOUNT_SCHEMA
    from tofu_db.generated_checkpoint_control import FRONTIER_OBSERVATION_SCHEMA
    return tuple(Draft202012Validator(schema) for schema in (
        FRONTIER_OBSERVATION_SCHEMA, SHARED_ACCOUNT_SCHEMA))


def read_registered_actor_frontier(storage, admission, *, owner_user_id,
        conversation_id, parent_task_id, header_bytes, snapshot_bytes,
        identity_bytes, io_timeout_ms):
    """Return exact source evidence; aggregate admission remains the caller's duty."""
    from lib.identity import require_user_id
    owner = require_user_id(owner_user_id, context='registered actor preflight')
    binding = completion_binding(admission)
    if (admission.get('kind') != 'agent' or binding is None
            or not conversation_id or not parent_task_id
            or binding['identity']['user_id'] != owner
            or binding['identity']['conversation_id'] != conversation_id
            or binding['identity']['parent_task_id'] != parent_task_id
            or binding.get('account_root_task_id') != parent_task_id
            or any(type(value) is not int or value <= 0 for value in (
                header_bytes, snapshot_bytes, identity_bytes, io_timeout_ms))):
        raise RecoveryRefused('Actor preflight requires its exact owned source launch and bounds')
    try:
        before = _record(storage, binding['identity'])
        if type(before.get('version')) is not int or before['version'] < 1:
            raise RecoveryRefused('Actor registration has no exact persisted version')
        if INVOCATION_COMPLETION_FIELD in before['value']:
            raise RecoveryRefused('Completed actors require their immutable completion receipt')
        if not storage.native_checkpoint_available(user_id=owner):
            raise RecoveryRefused('Selected authority has no native checkpoint preflight')
        response = storage.native_checkpoint_control('frontier', dict(format=FORMAT, frontier=dict(
            owner_user_id=owner, task_id=admission['invocation_id'], header_bytes=header_bytes,
            snapshot_bytes=snapshot_bytes, identity_bytes=identity_bytes)), user_id=owner,
            deadline_unix_ms=time.time_ns() // 1000000 + io_timeout_ms,
            response_bytes=header_bytes + snapshot_bytes + RESPONSE_HEADROOM_BYTES)
        if response.get('accepted') is not True or response.get('capability_id') is not None:
            raise RecoveryRefused('Native actor frontier is not safely observable')
        frontier = response.get('state')
        observation_validator, account_validator = _validators()
        observation_validator.validate(frontier)
        state = frontier['snapshot']; account = state['shared_model_account']
        account_validator.validate(account)
        if (frontier['owner_user_id'] != owner or frontier['task_id'] != admission['invocation_id']
                or any(state.get(key) != frontier[key] for key in ('owner_user_id','task_id','epoch','binding'))
                or state.get('format') != 'tofu.native-agent-checkpoint/v1'
                or (frontier['phase'] == 'tools_pending') != (state.get('tools_pending') is True)
                or account['owner_user_id'] != owner or account.get('actor_task_id') != frontier['task_id']
                or account['root_task_id'] != binding['account_root_task_id']
                or account['complete'] is not True or 'root_tokens' in account
                or account['control_tool_calls'] != account['tool_calls']):
            raise RecoveryRefused('Native actor frontier belongs to another execution account')
        if _record(storage, binding['identity']) != before:
            raise RecoveryRefused('Actor registration changed during native preflight')
        return deepcopy({'admission': admission, 'frontier': frontier,
                         'registration_version': before['version']})
    except RecoveryRefused:
        raise
    except Exception as exc:
        raise RecoveryRefused('Native actor frontier could not be verified') from exc


def preflight_registered_actor_accounts(storage, account_frontier, admissions, *, consumed_actors=None, **options):
    """Match the entire live actor set to one durable shared-account cut.

    Closed actors are already included in the cut's scalar prefixes. Individual
    actor counters are compared, never added to that cumulative total. The Goal
    owner must still revalidate this version and admit successor lineage before
    any account/lease restoration; this observation grants neither.
    """
    from lib.agent_core.account_frontier import validate_frontier
    try:
        cut = validate_frontier(account_frontier)
        snapshot = cut['snapshot']; root = snapshot['root_task_id']
        actors = {key: value for key, value in cut['actors'].items() if key != root}
        rows = {row['invocation_id']: row for row in admissions}
        if (len(rows) != len(admissions) or set(rows) != set(actors)
                or snapshot['owner_user_id'] != options['owner_user_id']
                or root != options['parent_task_id']
                or not snapshot['settled'] or not snapshot['usage_known']
                or cut['model_requests'] or cut.get('recovery', {}).get('pending_actors')
                or any(row['parent_task_id'] != root for row in actors.values())):
            raise RecoveryRefused('Aggregate account lacks its exact settled direct actor set')
        observed = {}
        for identity, admission in rows.items():
            value = read_registered_actor_frontier(storage, admission, **options)
            incoming = (consumed_actors or {}).get(identity, {})
            producer = (value['frontier']['snapshot'].get('tool_recovery') or {}).get('producer_task_id')
            if incoming.get('effect_origin') and producer and producer != identity:
                value['effect_origin'] = deepcopy(incoming['effect_origin'])
            account = value['frontier']['snapshot']['shared_model_account']
            if (account['limits'] != snapshot['limits']
                    or any(account[key] != actors[identity][key] for key in (
                        'tokens', 'model_ordinal', 'tool_ordinal'))):
                raise RecoveryRefused('Native actor consumption changed after the shared account cut')
            charged = actors[identity]['tool_calls'] - account['tool_calls']
            if charged:
                value['pending_file_tools'] = _pending_file_tools(storage, value,
                    charged=charged, owner=options['owner_user_id'],
                    conversation_id=options['conversation_id'])
            observed[identity] = value
        return dict(account_frontier=cut, actors=observed)
    except RecoveryRefused:
        raise
    except Exception as exc:
        raise RecoveryRefused('Aggregate actor account preflight could not be verified') from exc


def _pending_file_tools(storage, observed, *, charged, owner, conversation_id):
    from lib.tasks_pkg.native_file_evidence import file_recovery_batch, file_recovery_occurrences
    from lib.tasks_pkg.invocation_effects import inspect_child_effect_batch
    frontier = observed['frontier']; state = frontier['snapshot']
    if (charged <= 0 or frontier['phase'] != 'tools_pending'
            or type(state.get('next_round')) is not int or state['next_round'] < 1):
        raise RecoveryRefused('Native actor consumption changed after the shared account cut')
    origin = observed.get('effect_origin', dict(task_id=frontier['task_id'],
        parent_task_id=state['shared_model_account']['root_task_id']))
    calls, references = file_recovery_batch(state, owner=owner,
        conversation_id=conversation_id, producer_task_id=origin['task_id'])
    if charged != len(calls):
        raise RecoveryRefused('Pending file batch does not explain its entire charged tool quota')
    observations = inspect_child_effect_batch(storage=storage, user_id=owner,
        conversation_id=conversation_id, invocation_id=origin['task_id'],
        registration_version=observed['registration_version'] if origin['task_id'] == frontier['task_id'] else None,
        expected_parent_task_id=origin['parent_task_id'],
        occurrences=file_recovery_occurrences(state, references))
    if any(row['result']['outcome'] not in {'file_published', 'file_not_published'} for row in observations):
        raise RecoveryRefused('Pending file operation remains unresolved')
    # Preserve the original native checkpoint and whole cut verbatim. This is
    # evidence of already-paid operations, never a new charge or replay permit.
    return dict(charged_tool_calls=charged, observations=observations)
