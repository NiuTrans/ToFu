"""Daily observation cannot revoke owner intent; verified success may heal access."""
from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import Future

import pytest
from lib.identity import PrincipalContext
from lib.model_routing import probe_automation as service

pytestmark = pytest.mark.unit


def fixture():
    ref = {'creator_id': 'maker', 'model_id': 'model'}
    cred = {'credential_id': 'c', 'authorization': {'models': [ref]}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::model': {'key_idx': 0, 'root_model_id': 'model',
                     'status': 'rate_limited', 'recommend_disable': True}}}
    return doc, bundle, snap


@pytest.mark.parametrize("status", ["rate_limited", "unavailable", "error", "unauthorized", "not_found"])
def test_failed_observation_never_revokes_existing_authorization(status):
    doc, bundle, snap = fixture()
    snap['cells']['0::model']['status'] = status
    doc['credentials'].append({'credential_id': 'other', 'authorization': {'models': [{'creator_id': 'maker', 'model_id': 'model'}]}})
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before
    assert len(doc['credentials'][1]['authorization']['models']) == 1
    assert service.apply_recommendations(doc, bundle, snap) == 0


def test_failed_wire_does_not_create_a_persistent_exclusion():
    # A successful sibling does not turn transient failure into durable policy.
    ref = {'creator_id': 'maker', 'model_id': 'model'}
    cred = {'credential_id': 'c', 'authorization': {'models': [ref]}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 2, 'done_count': 2, 'cells': {
        '0::wire-a': {'key_idx': 0, 'model_id': 'wire-a', 'root_model_id': 'model',
                      'status': 'ok', 'recommend_disable': False},
        '0::wire-b': {'key_idx': 0, 'model_id': 'wire-b', 'root_model_id': 'model',
                      'status': 'rate_limited', 'recommend_disable': True}}}
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc['credentials'][0]['authorization']['models'] == [ref]


def test_apply_mints_grant_for_ok_cell_without_grant():
    # Credential grants a different model; a fresh ok on an ungranted wire
    # mints a grant reaching that wire.
    ref = {'creator_id': 'maker', 'model_id': 'model'}
    cred = {'credential_id': 'c', 'authorization': {'models': []}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::model': {'key_idx': 0, 'model_id': 'model', 'root_model_id': 'model',
                     'status': 'ok', 'recommend_disable': False}}}
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [ref]


def test_apply_lifts_exclusion_on_fresh_ok():
    ref = {'creator_id': 'maker', 'model_id': 'model',
           'excluded_wire_model_ids': ['wire-a', 'wire-b']}
    cred = {'credential_id': 'c', 'authorization': {'models': [deepcopy(ref)]}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::wire-a': {'key_idx': 0, 'model_id': 'wire-a', 'root_model_id': 'model',
                      'status': 'ok', 'recommend_disable': False}}}
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [
        {'creator_id': 'maker', 'model_id': 'model',
         'excluded_wire_model_ids': ['wire-b']}]


def test_incomplete_probe_does_not_apply():
    doc, bundle, snap = fixture()
    snap['done_count'] = 0
    before = deepcopy(doc)
    with pytest.raises(ValueError):
        service.apply_recommendations(doc, bundle, snap)
    assert doc == before


@pytest.mark.parametrize('conflict', [False, True])
@pytest.mark.parametrize('observed_status', ['ok', 'rate_limited'])
def test_daily_run_uses_owner_and_revision(monkeypatch, conflict, observed_status):
    doc, bundle, snap = fixture()
    if observed_status == 'ok':
        doc['credentials'][0]['authorization']['models'] = []
        bundle['credentials'][0]['authorization']['models'] = []
    before = deepcopy(doc)
    snap['cells']['0::model'].update(status=observed_status, model_id='model')
    commits = []
    class Repository:
        def get(self, boundary):
            assert boundary.owner_user_id == 23
            return SimpleNamespace(document=doc, revision=9)
        def compare_and_swap(self, boundary, document, *, expected_revision):
            assert boundary.owner_user_id == 23
            assert expected_revision == 9
            if conflict:
                raise RuntimeError('revision changed')
            commits.append(document)
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: {
        'provider': {'base_url': 'https://example.test', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': {}})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [(0,)])
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', {})
    monkeypatch.setattr(service.probe, 'public_probe_snapshot', lambda task: task)
    def submit(task, work, timeout):
        task.update(deepcopy(snap))
        result = Future()
        result.set_result(None)
        return result
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task', submit)
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    if observed_status != 'ok':
        result = service.run_daily_probe(principal=principal, repository=Repository())
        assert result['grant_changes'] == 0
        assert commits == []
    elif conflict:
        with pytest.raises(RuntimeError, match='revision changed'):
            service.run_daily_probe(principal=principal, repository=Repository())
        assert not commits
    else:
        result = service.run_daily_probe(principal=principal, repository=Repository())
        assert result['grant_changes'] == 1
        assert commits[0]['credentials'][0]['authorization']['models'] == [{'creator_id': 'maker', 'model_id': 'model'}]
    assert doc == before


def test_missing_scope_denied_before_storage():
    with pytest.raises(PermissionError):
        service.run_daily_probe(principal=PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'scheduler:run'}))


def test_daily_schedule_defaults_and_async_dispatch(monkeypatch):
    from lib.scheduler.manager import ScheduledTaskManager
    manager = ScheduledTaskManager()
    captured = {}
    monkeypatch.setattr(manager, '_ensure_default_task', lambda **kw: (captured.update(kw) or ({'id': 'daily'}, True)))
    manager._ensure_default_provider_probe_task()
    assert captured['schedule'] == '0 8 * * *'
    assert captured['enabled'] is True
    assert captured['reconcile_enabled'] is False
    assert captured['max_runtime'] == service.DAILY_PROBE_MAX_RUNTIME
    called = []
    monkeypatch.setattr(manager, '_dispatch_maintenance_task', lambda task: called.append(task))
    manager._dispatch_claimed_task({'id': 'daily', 'task_type': 'provider_probe_daily'})
    assert len(called) == 1


def test_cancelled_run_never_starts_probe(monkeypatch):
    import threading
    doc, _, _ = fixture()
    stopped = threading.Event()
    stopped.set()
    repository = SimpleNamespace(get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: pytest.fail('must not plan'))
    with pytest.raises(TimeoutError):
        service.run_daily_probe(principal=principal, repository=repository, cancel_event=stopped)


def test_large_daily_matrix_is_submitted_without_count_refusal(monkeypatch):
    doc, bundle, snap = fixture()
    repository = SimpleNamespace(get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: {
        'provider': {'base_url': 'https://example.invalid', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': {}})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [()] * 5000)
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', {})
    submitted = []
    def submit(task, work, timeout):
        submitted.append(len(work))
        task.update(snap)
        result = Future(); result.set_result(None)
        return result
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task', submit)
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    service.run_daily_probe(principal=principal, repository=repository)
    assert submitted == [5000]


def test_daily_run_skips_provider_without_enabled_offerings(monkeypatch):
    from lib.model_routing.domain import ModelRoutingError
    doc, bundle, _ = fixture()
    repository = SimpleNamespace(
        get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)

    def _plan(*_a):
        raise ModelRoutingError(
            'provider access has no confirmed enabled offerings to probe',
            kind='provider_probe_no_offerings')

    monkeypatch.setattr(service, 'matrix_probe_plan', _plan)
    monkeypatch.setattr(
        service.probe, 'submit_provider_probe_task',
        lambda *_a, **_k: pytest.fail('must not submit'))
    principal = PrincipalContext.system(
        subject_id='test', owner_user_id=23, scopes={'providers'})
    result = service.run_daily_probe(
        principal=principal, repository=repository)
    assert result == {
        'ok': True, 'providers': 0, 'cells': 0, 'grant_changes': 0,
    }


@pytest.mark.parametrize('status', ['bad_request', 'invalid_response'])
def test_malformed_probe_does_not_revoke_model_grants(status):
    doc, bundle, snap = fixture()
    snap['cells']['0::model'].update(status=status,
        detail='HTTP 400 Unsupported parameter: _conversion_report')
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before


def test_neutral_sibling_prevents_removing_entire_model_grant():
    doc, bundle, snap = fixture()
    snap.update(total=2, done_count=2)
    snap['cells']['0::model']['model_id'] = 'wire-bad'
    snap['cells']['0::neutral'] = {
        'key_idx': 0, 'root_model_id': 'model', 'model_id': 'wire-neutral',
        'status': 'bad_request', 'recommend_disable': True}
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before



@pytest.mark.parametrize('reported_total', [1, 3])
def test_probe_cell_count_must_match_completed_count(reported_total):
    doc, bundle, snap = fixture()
    snap.update(total=reported_total, done_count=reported_total)
    snap['cells'] = {} if reported_total == 1 else snap['cells']
    before = deepcopy(doc)
    with pytest.raises(ValueError, match='incomplete provider probe'):
        service.apply_recommendations(doc, bundle, snap)
    assert doc == before


def test_legacy_disabled_sibling_cannot_disappear_from_removal_decision():
    doc, bundle, snap = fixture()
    snap.update(total=2, done_count=2)
    snap['cells']['0::model']['model_id'] = 'wire-bad'
    snap['cells']['0::legacy'] = {
        'key_idx': 0, 'root_model_id': 'model', 'model_id': 'wire-legacy',
        'status': 'not_found', 'recommend_disable': True,
        'deployment_enabled': False}
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before



def test_unprobed_connection_preserves_sibling_grant():
    doc, bundle, snap = fixture()
    bundle['offerings'][0]['offering_id'] = 'o'
    bundle['deployments'] = [
        {'offering_id': 'o', 'wire_model_id': 'bad-wire'},
        {'offering_id': 'o', 'wire_model_id': 'offline-wire'}]
    snap['cells']['0::model']['model_id'] = 'bad-wire'
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before


def test_new_positive_grant_excludes_failed_and_unprobed_sibling_wires():
    doc, bundle, snap = fixture()
    doc['credentials'][0]['authorization']['models'] = []
    bundle['credentials'][0]['authorization']['models'] = []
    bundle['offerings'][0]['offering_id'] = 'o'
    bundle['deployments'] = [
        {'offering_id': 'o', 'wire_model_id': wire}
        for wire in ('good', 'failed', 'unprobed')]
    snap.update(total=2, done_count=2)
    snap['cells'] = {
        'good': {'key_idx': 0, 'model_id': 'good', 'root_model_id': 'model', 'status': 'ok'},
        'failed': {'key_idx': 0, 'model_id': 'failed', 'root_model_id': 'model',
                   'status': 'unavailable', 'recommend_disable': True}}
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [{
        'creator_id': 'maker', 'model_id': 'model',
        'excluded_wire_model_ids': ['failed', 'unprobed']}]
