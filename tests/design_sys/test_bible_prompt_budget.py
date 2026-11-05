"""Bounded author/edit prompts retain both common and scenario guidance."""

import pytest

from lib.design_sys.themes import SCENARIOS, design_bible_text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('limit', [2500, 3500])
def test_bounded_bible_keeps_scenario_and_common_guidance(scenario, limit):
    specific = design_bible_text(scenario, include_general=False)
    excerpt = design_bible_text(scenario, limit=limit)

    assert len(excerpt) <= limit
    assert '# General Design Law' in excerpt
    assert specific.splitlines()[0] in excerpt
    assert specific.split('\n\n')[1] in excerpt


def test_single_bible_uses_available_budget():
    full = design_bible_text('academic-research', include_general=False)
    assert design_bible_text('academic-research', include_general=False,
                            limit=len(full)) == full
    assert len(design_bible_text('unknown', limit=3500)) <= 3500
