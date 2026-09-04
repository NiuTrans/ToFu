"""Localized model-price presentation policy.

The provider/model registration remains authoritative in its declared
currency; the UI converts only the visible values to the UI language's
currency. These specs pin the pure presentation policy (conversion, pivot
fail-closed, and per-language formatters) used by the model-routing v2
settings surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PRESENTATION_TS = (
    ROOT / 'frontend/src/features/settings/model-price-localization.ts')


_HARNESS = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const rates = { USD: 1, CNY: 7.2, JPY: 144, KRW: 1320 };
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="host"></div></body>',
  targets: [process.argv[2]],
  globals: {},
});

const presentation = global.modelPricePresentation;

try {
  // Pure locale policy: currently shipped zh/en plus future ja/ko language packs.
  check('zh_uses_cny', presentation.currencyForLanguage('zh-CN') === 'CNY');
  check('en_uses_usd', presentation.currencyForLanguage('en') === 'USD');
  check('ja_uses_jpy', presentation.currencyForLanguage('ja-JP') === 'JPY');
  check('ko_uses_krw', presentation.currencyForLanguage('ko-KR') === 'KRW');
  check('unknown_language_falls_back_usd',
    presentation.currencyForLanguage('fr') === 'USD');

  check('usd_to_cny_changes_amount',
    presentation.convert(1, 'USD', 'CNY', rates) === 7.2);
  check('cny_to_jpy_uses_usd_pivot',
    presentation.convert(7.2, 'CNY', 'JPY', rates) === 144);
  const roundTrip = presentation.convert(
    presentation.convert(3.45, 'USD', 'KRW', rates),
    'KRW', 'USD', rates);
  check('conversion_round_trip', Math.abs(roundTrip - 3.45) < 1e-9);
  check('missing_rate_fails_closed',
    presentation.convert(1, 'USD', 'JPY', { USD: 1 }) === null);
  check('missing_target_rate_keeps_usd_authority',
    presentation.displayCurrency('zh', 'USD', { USD: 1 }) === 'USD');
  check('missing_pivot_identity_displays_cny_authority',
    presentation.displayCurrency('zh', 'CNY', { USD: 1 }) === 'CNY');
  check('missing_pivot_keeps_cny_input_editable',
    presentation.inputValue(2, 'CNY', 'zh', { USD: 1 }) === '2');
  check('zh_formatter_emits_rmb_not_dollar',
    /[¥￥]/.test(presentation.formatForUi(1, 'USD', 'zh', rates))
    && presentation.formatForUi(1, 'USD', 'zh', rates).indexOf('$') < 0);
  check('ko_formatter_emits_won',
    presentation.formatForUi(1, 'USD', 'ko', rates).indexOf('₩') >= 0);
  check('cny_card_keeps_subunit_precision_above_100',
    presentation.formatForUi(49.72, 'USD', 'zh', rates).indexOf('.') >= 0);
} catch (error) {
  check('harness_threw:' + (error && error.stack || error), false);
} finally {
  report();
}
'''


def test_model_price_localization_ui_contract() -> None:
    presentation = native_module_path(
        'model-price-localization.js', PRESENTATION_TS)
    run_harness(
        target_js=presentation,
        body_js=_HARNESS,
        expect_pass=15,
        label='model-price-localization',
    )


def test_backend_display_policy_is_bounded_and_defensively_copied() -> None:
    from lib.pricing import get_model_price_display_policy
    import lib.pricing._refresh as refresh

    policy = get_model_price_display_policy()
    assert policy['base_currency'] == 'USD'
    assert set(policy['usd_rates']) == {'USD', 'CNY', 'JPY', 'KRW'}
    assert all(value > 0 for value in policy['usd_rates'].values())

    first = refresh.get_pricing_data()
    first['usdRates']['CNY'] = 999
    second = refresh.get_pricing_data()
    assert second['usdRates']['CNY'] != 999


def test_server_config_contract_names_exchange_rate_axes() -> None:
    """The live API field contract owns the response shape."""
    from tests.test_api_field_contract import _SERVER_CONFIG_SPEC

    display_spec = _SERVER_CONFIG_SPEC['model_price_display']
    assert set(display_spec) == {
        'base_currency', 'usd_rates', 'updated_at', 'source',
    }
    assert set(display_spec['usd_rates']) == {'USD', 'CNY', 'JPY', 'KRW'}


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
