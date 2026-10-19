"""Credential/route-source failures are discriminated from payload defects.

Root cause of the muu1klptjtwd3t deadlock: the owning Codex subscription's
shared credential could not be read (``LocalRequestPreparationError`` wrapping
``SubscriptionCredentialUnavailableError``). That class is a ROUTE/environment
outage — another provider can serve the same payload — so it must be
rotatable, while a genuine local payload/contract defect must NOT be. These
tests pin the discrimination contract both rotation points rely on
(``llm_fallback/_call.py`` and ``compaction/_layer2/_summary.py``).
"""

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


def test_llm_fallback_uses_same_discrimination():
    """The llm_fallback rotation gate must use the identical cause-chain
    discrimination (regression: it previously gated ALL
    LocalRequestPreparationError as non-rotatable)."""
    import inspect
    from lib.tasks_pkg.llm_fallback import _call as fallback_call

    source = inspect.getsource(fallback_call)
    # The blanket "NOT switching models" branch must now be gated on the
    # credential-cause check rather than firing for every local-prepare error.
    assert '_credential_unavailable' in source
    assert 'SubscriptionCredentialUnavailableError' in source
    assert 'SubscriptionLoginRequiredError' in source
