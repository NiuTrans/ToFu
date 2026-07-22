"""Gateway keyword-sanitization guards (lib.llm_sanitize._gateway).

The blocked-terms table currently ships IDENTITY placeholders (each term maps
to itself) because the real replacement values are owner-gated (a content/
policy call, verifiable only against the live corporate gateway). Until a real
value lands the sanitizer must be a faithful no-op AND must not emit a false
"Replaced N term(s)" log line. These tests pin both properties and — as a
NEUTER guard — prove the replacement machinery still fires the moment a genuine
(non-identity) value is supplied, so the module isn't a permanent dead no-op.

Run:  pytest tests/test_gateway_sanitize.py -m unit
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestGatewaySanitizeInert:
    """Current shipped state: identity placeholders → true no-op, no false log."""

    def test_all_shipped_entries_are_identity_placeholders(self):
        from lib.llm_sanitize._gateway import _GATEWAY_BLOCKED_TERMS
        # Documents the intentionally-inert state. When an owner supplies a
        # real replacement this assertion flips — that is the signal to delete
        # it and rely on the activation test below instead.
        assert all(k == v for k, v in _GATEWAY_BLOCKED_TERMS.items()), (
            'A non-identity replacement was added — remove this inert-state '
            'guard; the activation test now covers the live behavior.')

    def test_identity_term_passes_through_unchanged(self):
        from lib.llm_sanitize._gateway import _sanitize_gateway_content
        text = '关于习主席的新闻报道与 FLG 相关讨论'
        assert _sanitize_gateway_content(text) == text

    def test_identity_term_emits_no_false_replacement_log(self, caplog):
        from lib.llm_sanitize._gateway import _sanitize_gateway_content
        with caplog.at_level(logging.DEBUG, logger='lib.llm_sanitize._gateway'):
            _sanitize_gateway_content('习主席 江主席 赵总理 FLG QNS')
        assert not any('Replaced' in r.getMessage() for r in caplog.records), (
            'Identity placeholder produced a false "Replaced" log line — the '
            'sanitizer claimed to sanitize while changing nothing.')

    def test_empty_and_clean_text(self):
        from lib.llm_sanitize._gateway import _sanitize_gateway_content
        assert _sanitize_gateway_content('') == ''
        assert _sanitize_gateway_content(None) is None
        assert _sanitize_gateway_content('ordinary text') == 'ordinary text'


@pytest.mark.unit
class TestGatewaySanitizeActivation:
    """NEUTER guard: a genuine (non-identity) value MUST replace + log.

    Patches the table with a real substitution so the test proves the machinery
    works — if someone deletes the replacement loop this fails, showing the
    module is not a permanent dead no-op.
    """

    def test_real_replacement_fires(self, monkeypatch, caplog):
        from lib.llm_sanitize import _gateway
        monkeypatch.setattr(_gateway, '_GATEWAY_BLOCKED_TERMS',
                            {'BLOCKED_XYZ': 'SAFE_XYZ'})
        with caplog.at_level(logging.DEBUG, logger='lib.llm_sanitize._gateway'):
            out = _gateway._sanitize_gateway_content('pre BLOCKED_XYZ post')
        assert out == 'pre SAFE_XYZ post'
        assert any('Replaced' in r.getMessage() for r in caplog.records)

    def test_messages_list_string_and_blocks(self, monkeypatch):
        from lib.llm_sanitize import _gateway
        monkeypatch.setattr(_gateway, '_GATEWAY_BLOCKED_TERMS',
                            {'BLOCKED_XYZ': 'SAFE_XYZ'})
        messages = [
            {'role': 'user', 'content': 'a BLOCKED_XYZ b'},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'c BLOCKED_XYZ d'},
                {'type': 'image', 'source': {}},
            ]},
        ]
        out = _gateway._sanitize_messages(messages)
        assert out[0]['content'] == 'a SAFE_XYZ b'
        assert out[1]['content'][0]['text'] == 'c SAFE_XYZ d'
        assert out[1]['content'][1] == {'type': 'image', 'source': {}}
