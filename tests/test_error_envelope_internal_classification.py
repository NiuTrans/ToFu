"""Regression: a Python programming-error builtin (our own code bug) must be
classified as ``internal`` — NOT ``generic`` with the misleading
"Settings → Keys / 429 quota" hint.

Root cause (conv mrova3t92jffm7)
--------------------------------
A deterministic internal ``TypeError``
(``_ContentWithDisplayResults.__new__() missing 1 required positional
argument: 'display_results'``) reached the LLM-fallback error path. Because
``_classify_exception`` had no branch for programming-error builtins, it fell
through to ``generic`` — whose hint tells the user to "check Settings → Keys,
re-enable a 429-disabled key, or switch model / provider". That sent the user
chasing a non-existent quota problem for what was purely our bug.

The fix routes ``TypeError``/``AttributeError``/``KeyError``/… to the already
defined ``internal`` kind (hint: "check the server logs"). Dispatch-layer
``RuntimeError`` / bare ``Exception`` string-shaped errors are deliberately
NOT swept in — the substring heuristics must still classify them.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestInternalErrorClassification:

    def test_typeerror_classifies_internal(self):
        from lib.error_envelope._classify import _classify_exception
        exc = TypeError(
            "_ContentWithDisplayResults.__new__() missing 1 required "
            "positional argument: 'display_results'")
        assert _classify_exception(exc) == 'internal'

    @pytest.mark.parametrize('exc', [
        AttributeError("'NoneType' object has no attribute 'x'"),
        KeyError('missing'),
        IndexError('list index out of range'),
        NameError("name 'foo' is not defined"),
        UnboundLocalError('local var referenced before assignment'),
        ValueError('bad value'),
        AssertionError('invariant broke'),
        ZeroDivisionError('division by zero'),
    ])
    def test_programming_builtins_classify_internal(self, exc):
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(exc) == 'internal'

    def test_internal_envelope_hint_is_logs_not_quota(self):
        """The user-facing hint must point at server logs, NOT the
        Settings→Keys / 429 quota advice."""
        from lib.error_envelope import from_exception
        env = from_exception(
            TypeError('missing 1 required positional argument'),
            model='aws.claude-opus-4.7',
            context='both-failed (opus-4.8→opus-4.7)',
            source='llm-fallback')
        assert env['kind'] == 'internal'
        assert 'logs/error.log' in env['hint']
        assert 'Keys / Providers' not in env['hint']
        assert '429' not in env['hint']

    def test_neuter_dispatch_string_errors_still_classify(self):
        """NEUTER: the new branch must NOT swallow dispatch-layer errors that
        the substring heuristics own. A RuntimeError carrying a rate-limit /
        timeout / unreachable message must keep its specific kind, and a
        generic RuntimeError stays 'generic' (not 'internal')."""
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(RuntimeError('HTTP 429 too many requests')) == 'ratelimit'
        assert _classify_exception(RuntimeError('read timed out')) == 'timeout'
        assert _classify_exception(RuntimeError('all dispatch attempts failed')) == 'dispatch_exhausted'
        # A bare RuntimeError with no recognised substring is NOT a leaf
        # programming-defect builtin → stays generic (dispatch owns it).
        assert _classify_exception(RuntimeError('something opaque upstream')) == 'generic'
