"""Production projection trims optional schemas without trimming capabilities."""
import pytest

from evaluations.evolution.schema_cost import compare_schema_cost

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('budget', [1, 4000])
def test_coding_projection_keeps_authority_discovery_and_direct_protocols(budget):
    report = compare_schema_cost(owner_id=29, model='glm-5.3-flash', budget=budget)
    assert report['authority_equal'] and report['catalog_unchanged']
    assert report['direct_protocols_preserved']
    assert report['candidate']['missing_discovery'] == []
    assert report['control']['missing_discovery'] == []
    assert report['candidate']['estimated_schema_tokens'] < report['control']['estimated_schema_tokens']
    # Cost pressure cannot remove the direct terminal or handle-control ports.
    assert {'evolution_coordinate', 'evolution_artifact', 'poll', 'kill',
            'search_tools', 'execute_tools', 'run_command'}.issubset(
                report['candidate']['visible_names'])


def test_explicit_zero_is_an_exact_control_projection():
    report = compare_schema_cost(owner_id=29, model='glm-5.3-flash', budget=0)
    assert report['control'] == report['candidate']
