"""Executable specification for the canonical paper identity."""

from __future__ import annotations

import hashlib

import pytest


pytestmark = pytest.mark.unit


def test_paper_hash_is_whitespace_insensitive_and_deterministic():
    from lib.paper_identity import _paper_hash

    raw = 'the quick brown paper\n\n  '
    expected = hashlib.sha256(raw.strip().encode('utf-8')).hexdigest()[:32]
    assert _paper_hash(raw) == expected
    assert _paper_hash(raw) == _paper_hash(raw.strip())


@pytest.mark.parametrize('empty', ['', '   \n\t ', None])
def test_empty_paper_text_has_no_identity(empty):
    from lib.paper_identity import _paper_hash

    assert _paper_hash(empty) == ''


def test_client_hash_is_used_only_when_it_is_a_safe_canonical_key():
    from lib.paper_identity import _paper_hash, resolve_paper_hash

    assert resolve_paper_hash('a1b2c3d4e5f6', 'some text') == 'a1b2c3d4e5f6'
    assert resolve_paper_hash('', 'some text\n') == _paper_hash('some text\n')
    assert resolve_paper_hash(None, 'some text\n') == _paper_hash('some text\n')
    assert resolve_paper_hash('../../etc/passwd', 'some text') == _paper_hash(
        'some text')
    assert resolve_paper_hash('zz-not-hex', 'some text') == _paper_hash(
        'some text')
