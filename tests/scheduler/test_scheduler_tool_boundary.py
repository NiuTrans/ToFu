"""The scheduler tools' model-facing boundary.

Scheduled rows run with the authority their type implies: built-in types
(optimizer, provider_probe_daily, model_intelligence_daily, storage_backup,
reserve_reclaim, daily_report_backfill) and the Router Watchdog review execute
system-scoped or deployment-wide work. A model may therefore create or switch
only the four schedulable types, set only the documented update fields, and
never turn a row into (or out of) a built-in or service type.
"""
from __future__ import annotations

import pytest

import lib
import lib.scheduler.manager as manager_module
import lib.scheduler.proactive as proactive
from lib.scheduler.manager import ScheduledTaskManager
from lib.scheduler.tool_defs import (
    SCHEDULABLE_TASK_TYPES, SCHEDULE_TOOL_CREATE, SCHEDULE_TOOL_MANAGE, TOOL_UPDATABLE_FIELDS,
)
from tests.scheduler.test_scheduled_agent_condition_contract import (
    INSTRUCTION, OWNER, _create, _SchedulerStore, _tool,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store(monkeypatch):
    store = _SchedulerStore()

    def client(*, write=False):
        return store

    monkeypatch.setattr(manager_module, '_scheduler_client', client)
    monkeypatch.setattr(proactive, '_scheduler_client', client)
    return store

SERVICE_TYPES = ('optimizer', 'provider_probe_daily', 'model_intelligence_daily',
                 'storage_backup', 'reserve_reclaim', 'daily_report_backfill',
                 'router_watchdog_review')


def test_the_published_schema_states_the_same_boundary():
    create = SCHEDULE_TOOL_CREATE['function']['parameters']['properties']
    manage = SCHEDULE_TOOL_MANAGE['function']['parameters']['properties']
    assert tuple(create['task_type']['enum']) == SCHEDULABLE_TASK_TYPES
    assert manage['updates']['description'].endswith(', '.join(TOOL_UPDATABLE_FIELDS))


@pytest.mark.parametrize('task_type', SERVICE_TYPES)
def test_schedule_create_refuses_a_service_task_type(store, task_type):
    result = _tool('schedule_create', name='x', schedule='*/30 * * * *',
                   command='lib.optimizer.run_once()', task_type=task_type)

    assert result.startswith('Error:') and 'task_type' in result
    assert store.commands == []


@pytest.mark.parametrize('task_type', SERVICE_TYPES)
def test_an_update_cannot_turn_a_row_into_a_service_task(store, task_type):
    task = _create(task_type='prompt', command='Summarize the day.')

    result = _tool('schedule_manage', action='update', task_id=task['id'],
                   updates={'task_type': task_type})

    assert result.startswith('Error:') and 'task_type' in result
    assert store.rows[task['id']]['task_type'] == 'prompt'
    with pytest.raises(ValueError, match='task_type'):
        ScheduledTaskManager().update_task(task['id'], user_id=OWNER, task_type=task_type)


def test_a_service_row_keeps_its_type(store):
    task = _create(task_type='prompt', command='Summarize the day.')
    store.rows[task['id']]['task_type'] = 'storage_backup'

    with pytest.raises(ValueError, match='task_type'):
        ScheduledTaskManager().update_task(task['id'], user_id=OWNER, task_type='prompt')
    assert store.rows[task['id']]['task_type'] == 'storage_backup'


@pytest.mark.parametrize('field', ['poll_count', 'execution_count', 'tools_config',
                                   'target_conv_id', 'last_execution_status', 'enabled'])
def test_an_update_names_and_refuses_fields_outside_the_contract(store, field):
    task = _create(condition_command='release-watch')
    before = dict(store.rows[task['id']])

    result = _tool('schedule_manage', action='update', task_id=task['id'],
                   updates={'description': 'changed', field: 'x'})

    assert result.startswith('Error:') and field in result
    assert store.rows[task['id']] == before


def test_an_update_reports_exactly_what_it_applied(store):
    task = _create(condition_command='release-watch')

    result = _tool('schedule_manage', action='update', task_id=task['id'],
                   updates={'name': 'Renamed', 'description': 'New text'})

    assert result == f"Task {task['id']} updated: name, description"
    assert store.rows[task['id']]['name'] == 'Renamed'
    assert _tool('schedule_manage', action='update', task_id='missing',
                 updates={'name': 'x'}) == 'Error: Task not found'


def test_a_type_change_to_code_execution_honors_the_deployment_gate(store, monkeypatch):
    task = _create(task_type='prompt', command='echo hi')
    monkeypatch.setattr(lib, 'SCHEDULER_ALLOW_CODE_EXEC', False, raising=False)

    result = _tool('schedule_manage', action='update', task_id=task['id'],
                   updates={'task_type': 'command'})

    assert result.startswith('Error:') and 'SCHEDULER_ALLOW_CODE_EXEC' in result
    assert store.rows[task['id']]['task_type'] == 'prompt'


@pytest.mark.parametrize(('condition_command', 'condition_regex', 'message'), [
    ('release-watch', '([unclosed', 'invalid condition_regex'),
    ('', 'NEW', 'condition_regex requires condition_command'),
])
def test_an_agent_predicate_regex_is_validated_like_a_timers(store, condition_command,
                                                             condition_regex, message):
    result = _tool('schedule_create', name='x', schedule='*/30 * * * *', command=INSTRUCTION,
                   task_type='agent', target_conv_id='conv-intake',
                   condition_command=condition_command, condition_regex=condition_regex)

    assert result.startswith('Error:') and message in result
    assert store.commands == []
