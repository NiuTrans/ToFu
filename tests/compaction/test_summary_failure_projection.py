"""Only current issued summary failures may reach final admission or receipts.

Provider transport, source messages and credential material stay outside this
projection. The real budget, final admission and envelope boundaries run against
synthetic summary results; no provider or durable production store is accessed.
"""
from copy import copy, deepcopy
import json
from pathlib import Path
from unittest.mock import MagicMock

from jsonschema import Draft202012Validator
import pytest

from lib.error_envelope import from_exception
from lib.llm.llm_errors import (
    ContextCompactionError,
    LocalRequestPreparationError,
    SubscriptionCredentialUnavailableError,
)
from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
from lib.tasks_pkg.compaction._summary_budget import SummaryBudget

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = Draft202012Validator(json.loads(
    (ROOT / 'contracts/compaction_receipt_v1.schema.json').read_text()))
REQUEST_KEY = 'a' * 64
NEXT_REQUEST_KEY = 'b' * 64
PRIVATE_SOURCE = 'PRIVATE SOURCE, NOT SUMMARY FAILURE EVIDENCE'


def _failure(**changes):
    return {
        'code': 'summary_credentials_unavailable',
        'authIssue': 'codex_command_unavailable',
        'credentialSource': 'local',
        'providerReached': False,
        **changes,
    }


def _attempt(**changes):
    return {
        'modelId': 'summary-model', 'providerId': 'summary-provider',
        'status': 'failed', 'providerReached': False,
        'authIssue': 'codex_command_unavailable', 'credentialSource': 'local',
        **changes,
    }


def _receipt(usage=None, **kwargs):
    return build_compaction_receipt(
        trigger='dispatch_guard', status='failed', strategy='unchanged',
        implementation='summary_dispatch', summary_usage=usage, **kwargs)


def _failed_budget(task, *, failure=None, attempts=None, request_key=REQUEST_KEY):
    failure = _failure() if failure is None else failure
    if attempts is None:
        attempts = [_attempt(authIssue=failure['authIssue'],
                             credentialSource=failure['credentialSource'])]
    budget = SummaryBudget(task, request_key=request_key)
    budget.finish(False, failure_reason='summary_credentials_unavailable',
                  failure_evidence=failure, attempts=attempts)
    usage = {}
    budget.project_usage(usage)
    return budget, usage


def test_receipt_projects_safe_failure_and_attempts_without_source_material():
    usage = {'_summaryFailure': _failure(), '_summaryAttempts': [_attempt()],
             '_summaryRequestKey': REQUEST_KEY, '_privateError': PRIVATE_SOURCE}
    original = deepcopy(usage)
    receipt = _receipt(usage)
    assert receipt['summary']['failure'] == _failure()
    assert receipt['summary']['attempts'] == [_attempt()]
    assert receipt['summary']['complete'] is False
    assert receipt['summary']['accepted'] is False
    assert receipt['summary']['cost']['known'] is False
    assert receipt['summary']['cost']['costUsd'] is None
    VALIDATOR.validate(receipt)
    encoded = json.dumps(receipt, allow_nan=False)
    assert PRIVATE_SOURCE not in encoded and REQUEST_KEY not in encoded
    assert len(encoded.encode()) < 32 * 1024
    receipt['summary']['failure']['authIssue'] = 'modified'
    receipt['summary']['attempts'][0]['modelId'] = 'modified'
    assert usage == original


@pytest.mark.parametrize('failure', [
    None, [], 1, {}, _failure(code='summary_dispatch_failed'),
    _failure(authIssue=PRIVATE_SOURCE), _failure(authIssue='account_changed'),
    _failure(authIssue='codex_source_changed'),
    _failure(authIssue='credential_source_binding'),
    _failure(credentialSource='desktop_codex'), _failure(credentialSource={}),
    _failure(providerReached=True), _failure(providerReached=0),
    _failure(message=PRIVATE_SOURCE), _failure(error=PRIVATE_SOURCE),
    _failure(credentialReference={'access_token': PRIVATE_SOURCE}),
])
def test_receipt_rejects_unallowlisted_or_secret_failure_fields(failure):
    receipt = _receipt({'_summaryFailure': failure})
    assert 'failure' not in receipt['summary']
    assert PRIVATE_SOURCE not in json.dumps(receipt, allow_nan=False)
    VALIDATOR.validate(receipt)


@pytest.mark.parametrize('attempts', [
    [_attempt()] * 4, (_attempt(),), [None],
    [_attempt(modelId=[])], [_attempt(providerId=True)],
    [_attempt(modelId='x' * 201)], [_attempt(providerId='line\nsecret')],
    [_attempt(status='pending')], [_attempt(providerReached=0)],
    [_attempt(authIssue=PRIVATE_SOURCE)], [_attempt(providerReached=True)],
    [_attempt(status='succeeded')], [_attempt(credentialSource='unknown')],
    [_attempt(credentialReference=PRIVATE_SOURCE)], [_attempt(error=PRIVATE_SOURCE)],
])
def test_receipt_rejects_forged_attempts_and_does_not_trim_away_invalid_evidence(attempts):
    receipt = _receipt({'_summaryAttempts': attempts})
    assert 'attempts' not in receipt['summary']
    assert PRIVATE_SOURCE not in json.dumps(receipt, allow_nan=False)
    VALIDATOR.validate(receipt)


def test_attempts_do_not_invent_provider_ingress_for_unknown_local_faults():
    attempts = [
        {'modelId': 'summary-model', 'providerId': 'summary-provider', 'status': 'failed'},
        {'modelId': 'alternate-model', 'providerId': 'alternate-provider',
         'status': 'succeeded', 'providerReached': True},
    ]
    receipt = _receipt({'_summaryAttempts': attempts})
    assert receipt['summary']['attempts'] == attempts
    assert 'providerReached' not in receipt['summary']['attempts'][0]
    VALIDATOR.validate(receipt)


@pytest.mark.parametrize('summary_fields', [
    {'failure': _failure(authIssue='account_changed')},
    {'failure': _failure(authIssue=PRIVATE_SOURCE)},
    {'failure': _failure(providerReached=True)},
    {'failure': _failure(providerReached=0)},
    {'failure': _failure(credentialSource='unknown')},
    {'failure': _failure(credentialReference=PRIVATE_SOURCE)},
    {'attempts': [_attempt()] * 4},
    {'attempts': [_attempt(status='succeeded')]},
    {'attempts': [_attempt(providerReached=True)]},
    {'attempts': [_attempt(error=PRIVATE_SOURCE)]},
    {'attempts': [_attempt(modelId='x' * 201)]},
])
def test_schema_rejects_false_auth_certainty_unbounded_attempts_and_secrets(summary_fields):
    receipt = _receipt()
    receipt['summary'].update(summary_fields)
    assert not VALIDATOR.is_valid(receipt)


def test_legacy_receipt_does_not_infer_failure_or_attempt_count():
    receipt = _receipt()
    assert 'failure' not in receipt['summary']
    assert 'attempts' not in receipt['summary']
    VALIDATOR.validate(receipt)


def test_budget_reuses_one_keyed_failure_and_publishes_detached_safe_metadata():
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'config': {}}
    failure, attempts = _failure(), [_attempt()]
    original = deepcopy((failure, attempts))
    first, usage = _failed_budget(task, failure=failure, attempts=attempts)
    assert first.is_set()
    second = SummaryBudget(task, request_key=REQUEST_KEY)
    assert second.task is first.task
    second.finish(False)  # A suppressed repeat retains the first useful cause.
    repeated_usage = {}
    second.project_usage(repeated_usage)
    assert repeated_usage == usage
    cause = reconstructed_summary_failure(task, summary_usage=repeated_usage)
    assert isinstance(cause, SubscriptionCredentialUnavailableError)
    assert cause.reason == 'codex_command_unavailable'
    assert cause.credential_source == 'local' and cause.transient is True
    assert (failure, attempts) == original
    usage['_summaryFailure']['authIssue'] = PRIVATE_SOURCE
    usage['_summaryAttempts'][0]['modelId'] = PRIVATE_SOURCE
    second.project_usage(repeated_usage)
    assert repeated_usage['_summaryFailure'] == _failure()
    assert repeated_usage['_summaryAttempts'] == [_attempt()]


def test_new_key_and_late_old_budget_cannot_revive_old_failure():
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'config': {}}
    first, old_usage = _failed_budget(task)
    current = SummaryBudget(task, request_key=NEXT_REQUEST_KEY)
    assert current.task is not first.task and not current.is_set()
    assert reconstructed_summary_failure(task, summary_usage=old_usage) is None
    first.finish(False, failure_reason='summary_credentials_unavailable',
                 failure_evidence=_failure(), attempts=[_attempt()])
    first.project_usage(old_usage)
    assert '_summaryFailure' not in old_usage and '_summaryRequestKey' not in old_usage
    assert not current.is_set()
    assert '_summaryFailure' not in current.task


def test_budget_success_and_owner_cancellation_withdraw_cached_credential_cause():
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'config': {}}
    budget, usage = _failed_budget(task)
    succeeded = [{'modelId': 'alternate-model', 'providerId': 'alternate-provider',
                  'status': 'succeeded', 'providerReached': True}]
    budget.finish(True, attempts=succeeded)
    budget.project_usage(usage)
    assert '_summaryFailure' not in usage and not budget.is_set()
    assert usage['_summaryAttempts'] == succeeded
    assert reconstructed_summary_failure(task, summary_usage=usage) is None
    stopped = SummaryBudget(task, lambda: True, request_key=NEXT_REQUEST_KEY)
    stopped.finish(False, failure_reason='summary_credentials_unavailable',
                   failure_evidence=_failure(), attempts=[_attempt()])
    stopped.project_usage(usage)
    assert stopped.task['_summaryBudgetFailure'] == 'summary_cancelled'
    assert '_summaryFailure' not in usage
    assert reconstructed_summary_failure(task, summary_usage=usage) is None


@pytest.mark.parametrize('failure,attempts', [
    (_failure(error=PRIVATE_SOURCE), [_attempt()]),
    (_failure(authIssue='account_changed'), [_attempt()]),
    (_failure(), [_attempt(credentialReference=PRIVATE_SOURCE)]),
    (_failure(), [_attempt()] * 4),
])
def test_budget_does_not_authorize_unvalidated_failure_or_attempts(failure, attempts):
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'config': {}}
    budget, usage = _failed_budget(task, failure=failure, attempts=attempts)
    assert reconstructed_summary_failure(task, summary_usage=usage) is None
    assert PRIVATE_SOURCE not in json.dumps(budget.task)
    assert PRIVATE_SOURCE not in json.dumps(usage)


@pytest.mark.parametrize('forgery', ['plain_state', 'config', 'copied_state',
                                     'deepcopied_state', 'foreign_task', 'mutated_failure',
                                     'mutated_key', 'old_usage_key', 'missing_current_usage'])
def test_reconstruction_requires_issued_current_task_key_and_unmodified_evidence(forgery):
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'config': {}}
    budget, usage = _failed_budget(task)
    if forgery == 'plain_state':
        task['_summaryAttemptBudget'] = dict(budget.task)
    elif forgery == 'config':
        task['config']['_summaryAttemptBudget'] = task.pop('_summaryAttemptBudget')
    elif forgery == 'copied_state':
        task['_summaryAttemptBudget'] = copy(budget.task)
    elif forgery == 'deepcopied_state':
        task['_summaryAttemptBudget'] = deepcopy(budget.task)
    elif forgery == 'foreign_task':
        task = dict(task)
    elif forgery == 'mutated_failure':
        budget.task['_summaryFailure']['authIssue'] = 'codex_home_unavailable'
        usage['_summaryFailure']['authIssue'] = 'codex_home_unavailable'
    elif forgery == 'mutated_key':
        budget.task['requestKey'] = NEXT_REQUEST_KEY
        usage['_summaryRequestKey'] = NEXT_REQUEST_KEY
    elif forgery == 'old_usage_key':
        usage['_summaryRequestKey'] = NEXT_REQUEST_KEY
    else:
        usage = None
    assert reconstructed_summary_failure(task, summary_usage=usage) is None


@pytest.fixture
def admission_boundary(monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission

    task = {'id': 'projection-task', 'convId': 'projection-conversation',
            '_userId': 7, 'config': {'model': 'main-model'}}
    messages = [{'role': 'system', 'content': 'policy'},
                {'role': 'user', 'content': PRIVATE_SOURCE},
                {'role': 'assistant', 'content': 'required source evidence'},
                {'role': 'user', 'content': 'continue'}]
    original = deepcopy(messages)
    original_ids = [id(message) for message in messages]
    main_provider = MagicMock()
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', main_provider)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', main_provider)
    monkeypatch.setattr(admission, 'refresh_turn_requirements', lambda *a, **k: None)
    monkeypatch.setattr(admission, 'restore_checklist_context', lambda *a, **k: None)
    monkeypatch.setattr(admission, 'audit_log', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.try_pressure_l1',
                        lambda *a, **k: False)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.retain_oversized_tool_batch',
                        lambda *a, **k: False)
    monkeypatch.setattr('lib.llm_dispatch.native_context_scope.native_compaction_route_ready',
                        lambda *a, **k: True)
    monkeypatch.setattr(admission, '_measure', lambda *a, **k: {
        'totalTokens': 120_000, 'messageTokens': 119_000, 'toolSchemaTokens': 1000,
        'method': 'synthetic', 'messageCount': len(messages)})

    def ceiling(task, **kwargs):
        task['_contextBudgetDecision'] = {
            'contextWindowTokens': 128_000, 'windowSafetyTokens': 100_000,
            'resolvedOutputTokens': 8192}
        return 100_000, 100_000

    monkeypatch.setattr(admission, '_resolved_ceiling', ceiling)
    return admission, task, messages, original, original_ids, main_provider


@pytest.mark.parametrize('source,issue,transient', [
    ('local', 'codex_home_unavailable', True),
    ('desktop', 'desktop_disconnected', True),
    ('tofu_oauth', 'credential_source_unavailable', True),
    ('local', 'codex_credentials_unreadable', False),
])
def test_final_admission_refuses_oversized_source_with_current_typed_credential_cause(
        admission_boundary, monkeypatch, source, issue, transient):
    admission, task, messages, original, original_ids, main_provider = admission_boundary

    def fail_summary(*args, **kwargs):
        _budget, usage = _failed_budget(task, failure=_failure(
            credentialSource=source, authIssue=issue))
        kwargs['_result_meta'].update(summaryFailureReason='summary_credentials_unavailable',
                                      summary_usage=usage)
        return False

    monkeypatch.setattr(admission, 'force_compact_if_needed', fail_summary)
    with pytest.raises(LocalRequestPreparationError) as raised:
        admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=3, model='main-model')
    error = raised.value
    assert error.provider_reached is False and error.stage == 'prompt_admission_summary'
    assert isinstance(error.__cause__, SubscriptionCredentialUnavailableError)
    assert error.__cause__.reason == issue and error.__cause__.credential_source == source
    assert error.__cause__.transient is transient
    envelope = from_exception(error, source='prompt_admission')
    assert envelope['kind'] == 'generic' and envelope['severity'] == 'warning'
    assert envelope['retryable'] is transient
    assert envelope['authIssue'] == issue and envelope['credentialSource'] == source
    assert envelope['providerReached'] is False
    assert envelope['failureStage'] == 'subscription_credentials'
    assert PRIVATE_SOURCE not in json.dumps(envelope)
    assert task['_lastPromptAdmission']['action'] == 'refuse_summary_credentials'
    assert messages == original and [id(message) for message in messages] == original_ids
    main_provider.assert_not_called()


@pytest.mark.parametrize('current_failure', ['generic_pipeline', 'forged_state', 'old_usage'])
def test_final_admission_does_not_revive_old_or_forged_auth_causes(
        admission_boundary, monkeypatch, current_failure):
    admission, task, messages, original, original_ids, main_provider = admission_boundary
    _budget, old_usage = _failed_budget(task)
    if current_failure == 'forged_state':
        task['_summaryAttemptBudget'] = dict(task['_summaryAttemptBudget'])
    elif current_failure == 'old_usage':
        SummaryBudget(task, request_key=NEXT_REQUEST_KEY)

    def fail_summary(*args, **kwargs):
        kwargs['_result_meta'].update(summaryFailureReason='summary_pipeline_exception')
        if current_failure != 'generic_pipeline':
            kwargs['_result_meta']['summary_usage'] = old_usage
        return False

    monkeypatch.setattr(admission, 'force_compact_if_needed', fail_summary)
    with pytest.raises(ContextCompactionError) as raised:
        admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=3, model='main-model')
    assert raised.value.__cause__ is None
    envelope = from_exception(raised.value)
    assert envelope['kind'] == 'internal' and 'authIssue' not in envelope
    assert messages == original and [id(message) for message in messages] == original_ids
    main_provider.assert_not_called()


def test_credential_failure_with_physical_headroom_still_defers_without_losing_source(
        admission_boundary, monkeypatch):
    admission, task, messages, original, _original_ids, main_provider = admission_boundary

    def ceiling(task, **kwargs):
        task['_contextBudgetDecision'] = {
            'contextWindowTokens': 200_000, 'windowSafetyTokens': 140_000,
            'resolvedOutputTokens': 65_536}
        return 140_000, 100_000

    def fail_summary(*args, **kwargs):
        _budget, usage = _failed_budget(task)
        kwargs['_result_meta'].update(summaryFailureReason='summary_credentials_unavailable',
                                      summary_usage=usage)
        return False

    monkeypatch.setattr(admission, '_resolved_ceiling', ceiling)
    monkeypatch.setattr(admission, 'force_compact_if_needed', fail_summary)
    measured = admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=3, model='main-model')
    assert measured['totalTokens'] == 120_000
    assert task['_lastPromptAdmission']['action'] == 'defer_compaction_physical_fit'
    assert messages == original
    main_provider.assert_not_called()


def test_runtime_allowlist_obeys_the_receipt_authority():
    from lib.tasks_pkg.compaction._summary_failure import (
        MAX_SUMMARY_ATTEMPTS, SUMMARY_AUTH_ISSUES, SUMMARY_CREDENTIAL_SOURCES)

    schema = VALIDATOR.schema
    assert SUMMARY_AUTH_ISSUES == frozenset(schema['$defs']['summaryAuthIssue']['enum'])
    assert SUMMARY_CREDENTIAL_SOURCES == frozenset(
        schema['$defs']['summaryCredentialSource']['enum'])
    assert MAX_SUMMARY_ATTEMPTS == schema['properties']['summary']['properties']['attempts']['maxItems']


def test_failure_code_cannot_retain_an_arbitrary_equality_or_message_object():
    class ForgedCode:
        def __eq__(self, other):
            return True

        def __str__(self):
            return PRIVATE_SOURCE

    receipt = _receipt({'_summaryFailure': _failure(code=ForgedCode())})
    assert 'failure' not in receipt['summary']
    json.dumps(receipt, allow_nan=False)


@pytest.mark.parametrize('target', ['usage', 'state'])
def test_invalid_attempt_fields_cannot_match_an_empty_trusted_attempt_projection(target):
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'config': {}}
    budget, usage = _failed_budget(task, attempts=[])
    if target == 'usage':
        usage['_summaryAttempts'] = [{'error': PRIVATE_SOURCE}]
    else:
        budget.task['_summaryAttempts'] = [{'error': PRIVATE_SOURCE}]
    assert reconstructed_summary_failure(task, summary_usage=usage) is None


@pytest.mark.parametrize('failure_reason', [PRIVATE_SOURCE, {'error': PRIVATE_SOURCE}, None, True])
def test_budget_failure_reason_rejects_raw_or_untyped_values(failure_reason):
    task = {'config': {}}
    budget = SummaryBudget(task, request_key=REQUEST_KEY)
    budget.finish(False, failure_reason=failure_reason)
    assert budget.task['_summaryBudgetFailure'] == 'summary_dispatch_failed'
    assert PRIVATE_SOURCE not in json.dumps(budget.task)


def test_current_generic_pipeline_error_outranks_even_an_issued_credential_projection(
        admission_boundary, monkeypatch):
    admission, task, messages, original, _original_ids, main_provider = admission_boundary

    def fail_summary(*args, **kwargs):
        _budget, usage = _failed_budget(task)
        kwargs['_result_meta'].update(summaryFailureReason='summary_pipeline_exception',
                                      summary_usage=usage)
        return False

    monkeypatch.setattr(admission, 'force_compact_if_needed', fail_summary)
    with pytest.raises(ContextCompactionError) as raised:
        admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=3, model='main-model')
    assert raised.value.__cause__ is None
    assert from_exception(raised.value)['kind'] == 'internal'
    assert messages == original
    main_provider.assert_not_called()


@pytest.mark.parametrize('attempts', [
    [{'modelId': 'model', 'providerId': 'provider', 'status': 'failed'}],
    [{'modelId': 'model', 'providerId': 'provider', 'status': 'failed', 'providerReached': True}],
    [{'modelId': 'model', 'providerId': 'provider', 'status': 'succeeded', 'providerReached': True}],
])
def test_receipt_failure_cannot_overrule_unknown_ingress_or_observed_provider_work(attempts):
    receipt = _receipt({'_summaryFailure': _failure(), '_summaryAttempts': attempts})
    assert 'failure' not in receipt['summary']
    assert receipt['summary']['attempts'] == attempts
    VALIDATOR.validate(receipt)


def test_real_round_boundary_does_not_build_or_fallback_a_main_request_after_typed_refusal(
        admission_boundary, monkeypatch):
    from types import SimpleNamespace
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop

    admission, task, messages, original, _original_ids, main_provider = admission_boundary

    def fail_summary(*args, **kwargs):
        _budget, usage = _failed_budget(task)
        kwargs['_result_meta'].update(summaryFailureReason='summary_credentials_unavailable',
                                      summary_usage=usage)
        return False

    monkeypatch.setattr(admission, 'force_compact_if_needed', fail_summary)
    monkeypatch.setattr(root_loop, 'prepare_round_context', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.notice_decision',
                        lambda *a, **k: None)
    body_builder, fallback = MagicMock(), MagicMock()
    monkeypatch.setattr(root_loop, 'build_round_request', body_builder)
    monkeypatch.setattr(root_loop, 'run_llm_call_with_fallback', fallback)
    request = SimpleNamespace(task=task, messages=messages,
                              state=SimpleNamespace(model='main-model'),
                              tid='projection-task', project_path='',
                              project_enabled=False, cfg={})
    hooks = root_loop._RootLoopHooks(request)
    with pytest.raises(LocalRequestPreparationError) as raised:
        hooks._dispatch_open_round(3, [])
    assert raised.value.provider_reached is False
    assert isinstance(raised.value.__cause__, SubscriptionCredentialUnavailableError)
    body_builder.assert_not_called()
    fallback.assert_not_called()
    main_provider.assert_not_called()
    assert messages == original


@pytest.mark.parametrize('trigger', ['working_set', 'window', 'dispatch_guard'])
def test_failed_settlement_saves_redacted_receipt_without_adopting_source(
        monkeypatch, trigger):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    task = {'id': 'settlement-task', 'convId': 'settlement-conversation',
            '_userId': 7, 'config': {'model': 'main-model'}}
    messages = [{'role': 'user', 'content': PRIVATE_SOURCE}]
    original = deepcopy(messages)
    _budget, usage = _failed_budget(task)
    meta, events, saved = {}, [], []
    store = MagicMock()
    store.update_archive_summary.side_effect = lambda *a, **k: saved.append((a, deepcopy(k)))
    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda task, event: events.append(event))

    def decline(*args, **kwargs):
        kwargs['_result_meta'].update(
            compacted=False, tokens_before=120_000, msgs_before=len(messages),
            archive_id='fixture-archive', summary_usage=dict(usage),
            summaryFailureReason='summary_credentials_unavailable')
        return 'Messages preserved as-is.'

    monkeypatch.setattr(compact, 'execute_compact_tool', decline)
    assert not compact.force_compact_if_needed(
        messages, task, force=True, _compaction_trigger=trigger,
        _compaction_round=3, _result_meta=meta)
    assert reconstructed_summary_failure(task, summary_usage=meta['summary_usage']) is not None
    assert messages == original
    assert len(saved) == 1 and saved[0][0][0] == 'fixture-archive'
    assert saved[0][0][1] == '' and saved[0][1]['post_messages'] is None
    receipt = saved[0][1]['receipt']
    assert receipt['status'] == 'failed'
    assert receipt['outcomeReason'] == 'summary_credentials_unavailable'
    assert receipt['summary']['failure'] == _failure()
    assert receipt['summary']['attempts'] == [_attempt()]
    assert receipt['summary']['accepted'] is False
    assert receipt['summary']['cost']['known'] is False
    assert 'PRIVATE' not in json.dumps(receipt) and REQUEST_KEY not in json.dumps(receipt)
    VALIDATOR.validate(receipt)
    done = [event for event in events if event.get('type') == 'compaction_done']
    assert len(done) == 1 and done[0]['receipt'] == receipt


@pytest.mark.parametrize('boundary', ['reconstruction', 'admission', 'root'])
def test_owner_stop_after_summary_settlement_outranks_credential_failure(
        admission_boundary, monkeypatch, boundary):
    from types import SimpleNamespace
    from lib.llm.llm_errors import AbortedError
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop

    admission, task, messages, original, original_ids, main_provider = admission_boundary
    if boundary == 'reconstruction':
        _budget, usage = _failed_budget(task)
        task['aborted'] = True
        assert reconstructed_summary_failure(task, summary_usage=usage) is None
        assert messages == original
        main_provider.assert_not_called()
        return

    def fail_summary(*args, **kwargs):
        _budget, usage = _failed_budget(task)
        kwargs['_result_meta'].update(summaryFailureReason='summary_credentials_unavailable',
                                      summary_usage=usage)
        return False

    original_measure = admission._measure
    measurements = []

    def measure(*args, **kwargs):
        result = original_measure(*args, **kwargs)
        measurements.append(result)
        if len(measurements) == 2:
            task['aborted'] = True  # Stop wins after summary settlement, before refusal.
        return result

    monkeypatch.setattr(admission, 'force_compact_if_needed', fail_summary)
    monkeypatch.setattr(admission, '_measure', measure)
    body_builder, fallback = MagicMock(), MagicMock()
    if boundary == 'root':
        monkeypatch.setattr(root_loop, 'prepare_round_context', lambda *a, **k: None)
        monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.notice_decision',
                            lambda *a, **k: None)
        monkeypatch.setattr(root_loop, 'build_round_request', body_builder)
        monkeypatch.setattr(root_loop, 'run_llm_call_with_fallback', fallback)
        request = SimpleNamespace(task=task, messages=messages,
            state=SimpleNamespace(model='main-model'), tid='stop-projection-task',
            project_path='', project_enabled=False, cfg={})
        hooks = root_loop._RootLoopHooks(request)
        run = lambda: hooks._dispatch_open_round(3, [])
    else:
        run = lambda: admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=3, model='main-model')

    with pytest.raises(AbortedError) as raised:
        run()
    assert raised.value.__cause__ is None
    assert task['_lastPromptAdmission']['action'] != 'refuse_summary_credentials'
    state = task['_summaryAttemptBudget']
    assert state['_summaryBudgetFailure'] == 'summary_cancelled'
    assert '_summaryFailure' not in state and state.projected_failure is None
    assert messages == original and [id(message) for message in messages] == original_ids
    body_builder.assert_not_called()
    fallback.assert_not_called()
    main_provider.assert_not_called()
