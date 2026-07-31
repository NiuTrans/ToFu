"""Gateway keyword-sanitization guards (lib.llm_sanitize._gateway).

Strategy (owner-ratified 2026-07-26, board epic pt_871a26c73d494a83,
question-block answer "A: Invisible-separator insertion"): every blocked
term maps to itself with a ZERO-WIDTH SPACE (U+200B) inserted after its
first character. That breaks the corporate gateway's exact-substring match
while staying invisible to humans and meaning-identical to the LLM; if the
gateway ever normalizes the separator away, behavior degrades to a no-op
(the pre-activation inert state) — never to anything worse.

These tests pin three load-bearing properties of the SHIPPED map (a revert
to identity placeholders turns them red — that is the NEUTER):
  1. no identity entries (the sanitizer is no longer inert);
  2. no replacement value still contains its original term contiguously
     (else the exact-substring filter still trips);
  3. stripping U+200B from any replacement reproduces the original term
     exactly (semantic round-trip).
Plus the mechanism guards from the placeholder era (monkeypatched map) and
the honesty guard (identity entries never emit a false "Replaced" log).

Run:  pytest tests/test_gateway_sanitize.py -m unit
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ZWSP = '​'


@pytest.mark.unit
class TestShippedZwspMap:
    """The live, owner-ratified ZWSP replacement map."""

    def test_no_identity_entries(self):
        from lib.llm_sanitize._gateway import _GATEWAY_BLOCKED_TERMS
        assert all(k != v for k, v in _GATEWAY_BLOCKED_TERMS.items()), (
            'An identity (no-op) entry survived — the sanitizer is inert '
            'again; restore the ZWSP replacement.')

    def test_no_replacement_contains_its_original_contiguously(self):
        from lib.llm_sanitize._gateway import _GATEWAY_BLOCKED_TERMS
        for k, v in _GATEWAY_BLOCKED_TERMS.items():
            assert k not in v, (
                f'{k!r} still appears contiguously in its replacement — the '
                f'exact-substring gateway filter would still trip on it.')

    def test_semantic_round_trip(self):
        from lib.llm_sanitize._gateway import _GATEWAY_BLOCKED_TERMS
        for k, v in _GATEWAY_BLOCKED_TERMS.items():
            assert v.replace(_ZWSP, '') == k, (
                f'removing the invisible separator from {v!r} does not '
                f'reproduce {k!r} — the LLM-facing meaning drifted.')

    def test_invisible_break_shape(self):
        from lib.llm_sanitize._gateway import _invisible_break
        assert _invisible_break('ABC') == 'A' + _ZWSP + 'BC'
        assert _invisible_break('XY').replace(_ZWSP, '') == 'XY'

    def test_shipped_sanitize_fires_and_logs(self, caplog):
        from lib.llm_sanitize._gateway import _sanitize_gateway_content
        text = '关于习主席与 FLG 的报道'
        with caplog.at_level(logging.DEBUG, logger='lib.llm_sanitize._gateway'):
            out = _sanitize_gateway_content(text)
        assert out != text
        assert '习主席' not in out and 'FLG' not in out
        assert out.replace(_ZWSP, '') == text
        assert any('Replaced' in r.getMessage() for r in caplog.records), (
            'A real replacement must be observable in the debug log.')


@pytest.mark.unit
class TestBuildBodyProviderGate:
    """The ``_pid == 'example-corp'`` gate in build_body decided which providers
    get the gateway keyword sanitizer. The 2026-07-28 Claude → Anthropic-
    native migration created provider ``example-corp_anthropic`` (same aigc.example-corp
    .com gateway, different surface) — under exact-equality it silently LOST
    sanitization, re-exposing blocked-term conversations to intermittent
    HTTP 450. The gate must key on the gateway, not the provider id's exact
    spelling: any ``example-corp*`` provider rides the same gateway."""

    @staticmethod
    def _term_and_broken():
        from lib.llm_sanitize._gateway import _GATEWAY_BLOCKED_TERMS
        k = next(iter(_GATEWAY_BLOCKED_TERMS))
        return k, _GATEWAY_BLOCKED_TERMS[k]

    def test_example-corp_anthropic_provider_gets_sanitized(self):
        from lib.llm.body import build_body
        term, broken = self._term_and_broken()
        body = build_body('claude-opus-5',
                          [{'role': 'user', 'content': f'报道 {term} 的新闻'}],
                          provider_id='example-corp_anthropic')
        text = body['messages'][0]['content']
        assert term not in text and broken in text, (
            'provider example-corp_anthropic rides the same your-llm-gateway.example.com '
            'gateway — its requests must get the ZWSP sanitizer')

    def test_example-corp_provider_still_sanitized(self):
        from lib.llm.body import build_body
        term, broken = self._term_and_broken()
        body = build_body('claude-opus-5',
                          [{'role': 'user', 'content': f'报道 {term} 的新闻'}],
                          provider_id='default')
        text = body['messages'][0]['content']
        assert term not in text and broken in text

    def test_unrelated_provider_not_sanitized(self):
        from lib.llm.body import build_body
        term, _broken = self._term_and_broken()
        body = build_body('kimi-k3',
                          [{'role': 'user', 'content': f'报道 {term} 的新闻'}],
                          provider_id='moonshot')
        assert term in body['messages'][0]['content'], (
            'a non-example-corp provider must NOT be mangled — the sanitizer is '
            'a gateway-specific workaround, not a global transform')


@pytest.mark.unit
class TestGatewaySanitizeMechanism:
    """Mechanism guards (monkeypatched map) — carried over from the
    placeholder era; they prove the replacement machinery itself works and
    that an identity entry can never lie about having replaced."""

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

    def test_identity_entry_emits_no_false_replacement_log(self, monkeypatch, caplog):
        """The honesty guard: an identity (no-op) entry must NOT produce a
        'Replaced' log line (the original placeholder-era bug)."""
        from lib.llm_sanitize import _gateway
        monkeypatch.setattr(_gateway, '_GATEWAY_BLOCKED_TERMS',
                            {'SAME_XYZ': 'SAME_XYZ'})
        with caplog.at_level(logging.DEBUG, logger='lib.llm_sanitize._gateway'):
            out = _gateway._sanitize_gateway_content('x SAME_XYZ y')
        assert out == 'x SAME_XYZ y'
        assert not any('Replaced' in r.getMessage() for r in caplog.records)

    def test_empty_and_clean_text(self):
        from lib.llm_sanitize._gateway import _sanitize_gateway_content
        assert _sanitize_gateway_content('') == ''
        assert _sanitize_gateway_content(None) is None
        assert _sanitize_gateway_content('ordinary text') == 'ordinary text'
