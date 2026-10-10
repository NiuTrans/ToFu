"""``parse_body(exact=...)`` is default-deny field admission: undeclared keys
fail loudly at the boundary instead of silently executing with defaults."""

from __future__ import annotations

import pytest

from lib.request_parser import BadRequest, _reject_unknown_keys

pytestmark = pytest.mark.unit


def test_exact_accepts_declared_subset_and_empty():
    assert _reject_unknown_keys({'a': 1}, ('a', 'b')) == {'a': 1}
    assert _reject_unknown_keys({}, ('a',)) == {}


def test_exact_rejects_undeclared_keys_with_names():
    with pytest.raises(BadRequest) as excinfo:
        _reject_unknown_keys({'a': 1, 'typo_field': 2, 'zz': 3}, ('a', 'b'))
    message = str(excinfo.value)
    assert 'typo_field' in message and 'zz' in message
    assert 'a,' not in message.replace('a: 1', '')


def test_empty_tuple_rejects_every_key():
    with pytest.raises(BadRequest):
        _reject_unknown_keys({'surprise': 1}, tuple())
