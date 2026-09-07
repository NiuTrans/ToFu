"""Executable specification for the static vendor release-date knowledge."""

from __future__ import annotations

import re

import pytest

from lib.model_info import release_date
from lib.model_info._release import _RULES


pytestmark = pytest.mark.unit


def test_canonical_ids_resolve_to_vendor_dates() -> None:
    assert release_date('glm-5.3') == '2026-08'  # vendor card dated 2026-08-14
    assert release_date('claude-opus-4-8') == '2026-08'
    assert release_date('kimi-k3') == '2026-07'
    assert release_date('gemini-3.7-flash') == '2026-08'


def test_wire_respellings_hit_the_trained_model_rule() -> None:
    # Bedrock-style region prefix with dot-folded version.
    assert release_date('aws.claude-opus-4.8') == '2026-08'
    # Vendor snapshot stamp suffix.
    assert release_date('doubao-seed-2-0-pro-260215') == '2026-02'
    # Case folding.
    assert release_date('MiniMax-M3') == '2026-06'


def test_unknown_models_return_none_instead_of_guessing() -> None:
    assert release_date('unseen-model-9000') is None
    assert release_date('') is None


def test_table_respects_granularity_and_specific_first_ordering() -> None:
    for needle, date in _RULES:
        assert re.fullmatch(r'\d{4}-\d{2}(-\d{2})?', date), (needle, date)
        assert needle == needle.lower() and '.' not in needle, needle
    # A dated variant must precede any prefix of it with a different date,
    # otherwise the generic rule would shadow the specific one.
    for later_index, (later, later_date) in enumerate(_RULES):
        for earlier, earlier_date in _RULES[:later_index]:
            if earlier in later and earlier_date != later_date:
                raise AssertionError(
                    f'{later!r} ({later_date}) must precede its prefix '
                    f'{earlier!r} ({earlier_date})')
