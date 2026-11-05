"""Credential/route-source failures are discriminated from payload defects.

Root cause of the muu1klptjtwd3t deadlock: the owning Codex subscription's
shared credential could not be read (``LocalRequestPreparationError`` wrapping
``SubscriptionCredentialUnavailableError``). That class is a ROUTE/environment
outage — another provider can serve the same payload — so it must be
rotatable, while a genuine local payload/contract defect must NOT be. A
mirrored predicate cannot detect drift in a real caller, so both rotation
points are driven behaviorally: the round fallback through
``_llm_call_with_fallback`` here, and the summary rotation point
(``compaction/_layer2/_summary.py``), sign-in included, by
``test_summary_credential_resilience.py``.
"""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _credential_error(transient=True):
    from lib.llm.llm_errors import (
        LocalRequestPreparationError,
        SubscriptionCredentialUnavailableError,
    )
    cause = SubscriptionCredentialUnavailableError(
        reason='codex_command_unavailable', credential_source='local',
        transient=transient)
    err = LocalRequestPreparationError(str(cause))
    err.__cause__ = cause
    return err


def _discriminator(exc):
    """Mirror of the shared predicate used by both rotation points."""
    from lib.llm.llm_errors import (
        LocalRequestPreparationError,
        SubscriptionCredentialUnavailableError,
        SubscriptionLoginRequiredError,
    )
    cause = getattr(exc, '__cause__', None)
    return (
        isinstance(exc, LocalRequestPreparationError)
        and isinstance(cause, (SubscriptionCredentialUnavailableError,
                               SubscriptionLoginRequiredError)))


def test_credential_outage_is_rotatable():
    from lib.llm.llm_errors import (
        LocalRequestPreparationError,
        SubscriptionLoginRequiredError,
    )
    login = LocalRequestPreparationError('x')
    login.__cause__ = SubscriptionLoginRequiredError('not signed in')
    assert _discriminator(_credential_error()) is True
    assert _discriminator(login) is True


def test_payload_defect_is_not_rotatable():
    from lib.llm.llm_errors import LocalRequestPreparationError
    # No typed subscription cause → genuine local body/contract bug.
    assert _discriminator(LocalRequestPreparationError('bad tool schema')) is False
    # Wrong exception type entirely.
    assert _discriminator(ValueError('x')) is False
    # A cause that is not a subscription error (e.g. conversion bug).
    err = LocalRequestPreparationError('x')
    err.__cause__ = RuntimeError('wire conversion failed')
    assert _discriminator(err) is False


def _sign_in_error():
    from lib.llm.llm_errors import (
        LocalRequestPreparationError,
        SubscriptionLoginRequiredError,
    )
    err = LocalRequestPreparationError('x')
    err.__cause__ = SubscriptionLoginRequiredError('not signed in')
    return err


def _untyped_defect():
    from lib.llm.llm_errors import LocalRequestPreparationError
    return LocalRequestPreparationError('bad tool schema')


def _foreign_cause_defect():
    from lib.llm.llm_errors import LocalRequestPreparationError
    err = LocalRequestPreparationError('x')
    err.__cause__ = RuntimeError('wire conversion failed')
    return err


@pytest.mark.parametrize('primary_error', [
    lambda: _credential_error(transient=False),
    lambda: _credential_error(transient=True),
    _sign_in_error,
    _untyped_defect,
    _foreign_cause_defect,
], ids=['credential_outage', 'transient_credential_outage', 'sign_in_required',
        'untyped_defect', 'foreign_cause_defect'])
def test_llm_fallback_uses_same_discrimination(monkeypatch, primary_error):
    """The real round fallback rotates exactly the failures the shared
    predicate calls credential outages (regression: it previously refused
    rotation for ALL LocalRequestPreparationError) and raises payload defects
    on the selected model."""
    from lib.llm.llm_errors import LocalRequestPreparationError
    from lib.tasks_pkg.llm_fallback import _call, _credential_recovery
    from tests.tasks_pkg.test_pool_rescue_fallback import (
        _base_task,
        _patch_common,
        _set_gate,
    )

    clock = [0.0]
    monkeypatch.setattr(_credential_recovery, 'time', SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    error = primary_error()
    calls = []

    def stream(task, body, **kwargs):
        calls.append(body['model'])
        if body['model'] == 'gpt-6-astra':
            raise error
        return ({'role': 'assistant', 'content': 'served by the fallback'},
                'stop', {'prompt_tokens': 5, 'completion_tokens': 3})

    _patch_common(monkeypatch, stream)
    _set_gate(monkeypatch, has=True)
    task = _base_task()
    args = (task, {'model': 'gpt-6-astra'}, 'gpt-6-astra', 1, 512, True, [],
            [{'role': 'user', 'content': 'synthetic'}], 'low', False, {}, [])

    if _discriminator(error):
        result = _call._llm_call_with_fallback(*args)
        assert calls[-1] == 'kimi-k3' and set(calls[:-1]) == {'gpt-6-astra'}
        assert result['model'] == 'kimi-k3'
        assert result['finish_reason'] == 'stop'
        assert 'error' not in task
    else:
        with pytest.raises(LocalRequestPreparationError) as raised:
            _call._llm_call_with_fallback(*args)
        assert raised.value is error
        assert calls == ['gpt-6-astra']
        assert not any(key in task for key in (
            '_fallback_model', '_fallback_from', '_fallback_reason'))
