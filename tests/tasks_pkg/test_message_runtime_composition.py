"""Communication integration against optional in-flight credential/delivery owners.

The clean committed profile may not contain these shared-runtime owners yet.
The actual shared-tree integration must execute, not skip, this specification.
"""
import pytest

pytestmark = pytest.mark.unit


def call_round(monkeypatch, stream, body, *, append_event=None):
    from lib.tasks_pkg.llm_fallback import _call as calls
    monkeypatch.setattr(calls, 'stream_llm_response', stream)
    monkeypatch.setattr(calls, '_get_fallback_model', lambda task: '')
    monkeypatch.setattr(calls, '_flag_empty_stop_for_retry', lambda *a: False)
    monkeypatch.setattr(calls, '_emit_round_usage', lambda *a, **k: None)
    monkeypatch.setattr(calls, 'append_event', append_event or (lambda *a: None))
    task = {'id': 'composition-test', 'convId': 'composition', 'config': {}}
    from lib.tasks_pkg.message_input_receipts import remember
    remember(task, [{'content': 'bounded original', '_projectMessageIds': ['msg']}])
    def run():
        return calls._llm_call_with_fallback(
            task, body, 'test-model', 0, 512, False, None,
            body.get('messages', []), 'low', False, {}, [])
    return task, run


def test_credential_retry_rechecks_receipt_authority(monkeypatch):
    recovery = pytest.importorskip('lib.tasks_pkg.llm_fallback._credential_recovery')
    from lib.llm.llm_errors import LocalRequestPreparationError, SubscriptionCredentialUnavailableError
    from lib.tasks_pkg import message_input_receipts as receipts
    revoked = False
    clock = [0.0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(recovery.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    validation = []
    def validate(task, matches):
        validation.append(matches)
        if revoked:
            raise PermissionError('scope revoked between credential attempts')
    monkeypatch.setattr(receipts, 'validate_matches', validate)
    dispatched = []
    def stream(*a, **k):
        dispatched.append(1)
        raise LocalRequestPreparationError('pre-ingress') from SubscriptionCredentialUnavailableError(
            reason='busy', credential_source='test', transient=True)
    def event(*a):
        nonlocal revoked
        revoked = True
    monkeypatch.setattr(recovery, 'append_event', event)
    task, run = call_round(monkeypatch, stream, {'messages': [{'role': 'user', 'content': 'bounded original'}]})
    with pytest.raises(PermissionError):
        run()
    assert dispatched == [1]
    assert len(validation) == 2
    assert '_verifiedMessageInputIds' not in task


@pytest.mark.parametrize('name', ['EventDeliveryError', 'EventDeliveryRevoked'])
def test_local_durability_cause_escapes_without_provider_replay(monkeypatch, name):
    delivery = pytest.importorskip('lib.tasks_pkg.event_delivery')
    from lib.tasks_pkg import message_input_receipts as receipts
    monkeypatch.setattr(receipts, 'validate_matches', lambda *a: None)
    original = getattr(delivery, name)('local durability cause')
    dispatched, phases = [], []
    def stream(*a, **k):
        dispatched.append(1)
        raise original
    task, run = call_round(monkeypatch, stream, {'messages': [{'content': 'bounded original'}]},
                           append_event=lambda *a: phases.append(a))
    with pytest.raises(type(original)) as caught:
        run()
    assert caught.value is original
    assert dispatched == [1]
    assert phases == []
    assert '_verifiedMessageInputIds' not in task
