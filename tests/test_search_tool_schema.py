"""Search-tool schema is GENERATED from tofu-search capability metadata.

Regression guard for the failure this replaced: the vertical list used to be a
hand-written paragraph in ``lib/tools/search.py``, so a domain added upstream
was invisible to the model until someone remembered to edit prose in this repo.
It also had no notion of credentials, so a domain whose key was missing was
still advertised and the model burned turns calling it.
"""

from __future__ import annotations

import pytest

from lib.tools.search import build_search_tool
from tofu_search import configure
from tofu_search.search.vertical import (describe_domains, list_domains,
                                         travel_flyai, travel_hotel)


@pytest.fixture(autouse=True)
def _restore_key():
    import tofu_search.config as _cfg
    saved = _cfg._global_config
    try:
        yield
    finally:
        _cfg._global_config = saved


def _vertical_enums(tool: dict) -> list[list[str]]:
    """Both places the enum appears: top-level and inside the batch array."""
    props = tool['function']['parameters']['properties']
    return [
        props['vertical']['enum'],
        props['queries']['items']['properties']['vertical']['enum'],
    ]


@pytest.mark.unit
def test_enum_is_derived_from_available_domains():
    configure(rollinggo_api_key='')
    expected = ['auto'] + list_domains() + ['off']
    for enum in _vertical_enums(build_search_tool()):
        assert enum == expected


@pytest.mark.unit
def test_every_available_domain_is_described_in_the_prose():
    configure(rollinggo_api_key='mcp_test')
    description = build_search_tool()['function']['description']
    for entry in describe_domains():
        assert f"``{entry['domain']}``" in description, entry['domain']


@pytest.mark.unit
def test_runtime_search_schema_stays_within_token_budget():
    """Loading vertical metadata must not duplicate enough prose to make the
    paper task's required tool floor exceed its 4k schema budget."""
    from lib.tools.gateway import tool_schema_tokens

    configure(rollinggo_api_key='mcp_test')
    tool = build_search_tool()
    assert tool_schema_tokens([tool], model='kimi-k3') <= 1_000


@pytest.mark.unit
def test_travel_is_advertised_and_names_both_capabilities():
    configure(rollinggo_api_key='mcp_test')
    tool = build_search_tool()
    for enum in _vertical_enums(tool):
        assert 'travel' in enum
    description = build_search_tool()['function']['description']
    assert 'flight' in description.lower()
    assert 'hotel' in description.lower()


@pytest.mark.unit
def test_full_keyless_availability_carries_no_warning():
    """Keyless: FlyAI's bundled trial credential covers BOTH travel types, so
    the travel line must NOT carry an availability warning."""
    configure(rollinggo_api_key='')
    description = build_search_tool()['function']['description']
    travel_line = next(line for line in description.splitlines()
                       if line.startswith('- ``travel``'))
    assert 'do NOT use' not in travel_line
    assert 'only flight' not in travel_line


@pytest.mark.unit
def test_partial_availability_warning_still_rendered(monkeypatch):
    """The gap-note mechanism itself stays load-bearing: if exactly one type
    loses its provider, the model must be told which queries to avoid."""
    configure(rollinggo_api_key='')
    monkeypatch.setattr(travel_hotel, 'is_available', lambda cfg=None: False)
    description = build_search_tool()['function']['description']
    travel_line = next(line for line in description.splitlines()
                       if line.startswith('- ``travel``'))
    assert 'only flight is available' in travel_line
    assert 'do NOT use this domain for hotel' in travel_line


@pytest.mark.unit
def test_no_availability_warning_once_the_key_is_present():
    configure(rollinggo_api_key='mcp_test')
    description = build_search_tool()['function']['description']
    travel_line = next(line for line in description.splitlines()
                       if line.startswith('- ``travel``'))
    assert 'do NOT use' not in travel_line


@pytest.mark.unit
def test_schema_is_rebuilt_per_call_not_frozen_at_import(monkeypatch):
    """Availability is PROCESS state — a rejected credential latches off
    mid-process — so caching the schema at import would lie."""
    configure(rollinggo_api_key='')
    before = build_search_tool()['function']['description']
    assert '``travel``' in before
    # Both travel types ride the FlyAI latch: once it flips, the whole domain
    # drops out of the schema.
    monkeypatch.setattr(travel_flyai, 'is_available', lambda cfg=None: False)
    after = build_search_tool()['function']['description']
    assert '``travel``' not in after
    assert before != after
