"""FRIDAY bills Claude cache tokens by its own multipliers of the base input.

学城 2762588777 《Friday 大模型平台 Claude 模型接入推荐实践》 §1.3 (verbatim):

    | 5m（默认） | × 1.25 base | $3.75 |
    | 1h | × 2.0 base | $6.00 |
    | 命中读取（任意 TTL） | × 0.1 base | $0.30 |

Anthropic's own price for Opus 5.5 cache reads is 0.05x, so FRIDAY rounds were
displayed at half their read charge. FRIDAY documents no base price; the base
input/output stay the creator's row. Every consumer resolves the same row:
the per-round/Turn cost stamps the conversation shows, the My Day calendar,
billing receipts and compaction economics. A direct (non-FRIDAY) route keeps
the creator's official row.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib.pricing import MODEL_PRICING, lookup_pricing

pytestmark = pytest.mark.unit

_FRIDAY = {'cacheWriteMul': 1.25, 'cacheWrite1hMul': 2.0, 'cacheReadMul': 0.10}
_FRIDAY_PROVIDER = 'example-corp'

# A recorded FRIDAY round (conversation worker-26744725b5250a5eb7bcea90 R111,
# trace 97d399fe34354616823efb58c6fc9f92): billing counters and dispatch
# identity exactly as persisted in apiRounds; diagnostics omitted.
RECORDED_FRIDAY_ROUND = {
    'model': 'claude-opus-5.5',
    'usage': {
        'input_tokens': 2,
        'cache_creation_input_tokens': 1324,
        'cache_read_input_tokens': 423341,
        'output_tokens': 1365,
        'prompt_tokens': 424667,
        'completion_tokens': 1365,
        'cache_creation': {'ephemeral_1h_input_tokens': 0,
                           'ephemeral_5m_input_tokens': 1324},
        '_dispatch': {'provider_id': 'example-corp',
                      'logical_model': 'claude-opus-5.5',
                      'model': 'kunlunbedrock.claude-opus-5.5-m17',
                      'protocol': 'anthropic'},
    },
}
_READ_TOKENS = 423341
_WRITE_TOKENS = 1324


def _stamped(row: dict) -> dict:
    from lib.tasks_pkg.round_costs import stamp_round_costs

    rounds = [copy.deepcopy(row)]
    stamp_round_costs({}, rounds, model='claude-opus-5.5')
    return rounds[0]['cost']


def test_recorded_friday_round_reads_cost_one_tenth_of_base_input():
    cost = _stamped(RECORDED_FRIDAY_ROUND)
    snapshot = cost['pricingSnapshot']
    assert (snapshot['provider'], snapshot['source']) == (_FRIDAY_PROVIDER, 'provider_override')
    assert snapshot['rates'] == {'input': 4.0, 'output': 20.0,
                                 'cacheWriteMul': 1.25, 'cacheReadMul': 0.10}
    assert cost['cacheReadCostUsd'] == pytest.approx(_READ_TOKENS * 4.0 * 0.10 / 1e6)
    assert cost['cacheWriteCostUsd'] == pytest.approx(_WRITE_TOKENS * 4.0 * 1.25 / 1e6)
    assert cost['outputCostUsd'] == pytest.approx(1365 * 20.0 / 1e6)


def test_friday_one_hour_writes_cost_twice_base_input():
    row = copy.deepcopy(RECORDED_FRIDAY_ROUND)
    row['usage']['cache_creation'] = {'ephemeral_1h_input_tokens': 1000,
                                      'ephemeral_5m_input_tokens': 324}
    cost = _stamped(row)
    assert cost['pricingSnapshot']['rates']['cacheWrite1hMul'] == 2.0
    assert cost['cacheWriteCostUsd'] == pytest.approx(
        (324 * 1.25 + 1000 * 2.0) * 4.0 / 1e6)


@pytest.mark.parametrize(('model_id', 'provider_id'), [
    ('claude-opus-5.5', None),
    ('claude-opus-5.5', 'anthropic'),
    ('claude-opus-5-5', 'anthropic'),
    ('claude-opus-5.5', 'catpaw'),
])
def test_non_friday_routes_keep_the_creators_official_row(model_id, provider_id):
    row = lookup_pricing(model_id, provider_id, prompt_tokens=0)
    official = MODEL_PRICING[model_id]
    assert row['_pricingSource'] == 'model_table'
    for field in ('input', 'output', 'cacheWriteMul', 'cacheReadMul'):
        assert row[field] == official[field], field
    assert row['cacheReadMul'] == 0.05
    assert 'cacheWrite1hMul' not in row


def test_every_claude_row_keeps_its_base_and_takes_fridays_cache_multipliers():
    from lib.model_info import is_claude

    claude_ids = sorted(model_id for model_id in MODEL_PRICING if is_claude(model_id))
    assert {'claude-opus-5.5', 'claude-opus-5-5', 'claude-sonnet-5-5'} <= set(claude_ids)
    for model_id in claude_ids:
        official = lookup_pricing(model_id, None, prompt_tokens=0)
        friday = lookup_pricing(model_id, _FRIDAY_PROVIDER, prompt_tokens=0)
        assert friday['_pricingSource'] == 'provider_override', model_id
        for field in ('input', 'output', 'currency', 'name'):
            assert friday.get(field) == official.get(field), (model_id, field)
        for field, multiplier in _FRIDAY.items():
            assert friday[field] == multiplier, (model_id, field)


def test_friday_rows_leave_non_claude_models_on_their_table_rows():
    row = lookup_pricing('kimi-k3.1-pro', _FRIDAY_PROVIDER, prompt_tokens=0)
    assert row['_pricingSource'] == 'model_table'
    assert row['cacheReadMul'] == MODEL_PRICING['kimi-k3.1-pro']['cacheReadMul']


def test_a_fresh_install_of_the_friday_template_gets_the_friday_rows():
    """Installing the YourProvider template creates the provider named by its key."""
    template = json.loads((Path(__file__).resolve().parents[2] / 'bundles' / 'yourprovider'
                           / 'provider_templates' / 'yourprovider.json').read_text('utf-8'))
    row = lookup_pricing('claude-opus-5.5', template['key'], prompt_tokens=0)
    assert row['_pricingSource'] == 'provider_override'
    assert row['cacheReadMul'] == 0.10


def test_every_cost_consumer_prices_the_recorded_round_identically():
    from lib.billing.cost import compute_request_cost
    from lib.cost import compute_api_rounds_cost
    from lib.daily_report.cost import _calc_msg_cost_cny
    from lib.tasks_pkg.compaction._summary_cost import _lookup_verified_pricing

    usage = RECORDED_FRIDAY_ROUND['usage']
    stamped = _stamped(RECORDED_FRIDAY_ROUND)
    # Turn total the conversation shows (lib/turn_lifecycle settled fold).
    turn = compute_api_rounds_cost([copy.deepcopy(RECORDED_FRIDAY_ROUND)],
                                   fallback_provider_id=_FRIDAY_PROVIDER)
    assert turn['costCny'] == pytest.approx(stamped['costCny'])
    # My Day calendar (prices conversation.turn_usage rows).
    assert _calc_msg_cost_cny(usage, 'claude-opus-5.5', _FRIDAY_PROVIDER) == pytest.approx(
        stamped['costCny'])
    # Billing receipt (micro-USD components, no margin).
    receipt = compute_request_cost('claude-opus-5.5', raw_usage=usage,
                                   provider_id=_FRIDAY_PROVIDER, margin=0.0)
    assert receipt.components['cache_read'] == round(_READ_TOKENS * 4.0 * 0.10)
    # Compaction summary receipts and cache economics.
    verified = _lookup_verified_pricing('claude-opus-5.5', _FRIDAY_PROVIDER,
                                        prompt_tokens=424667)
    assert (verified['cacheReadMul'], verified['cacheWriteMul']) == (0.10, 1.25)
