"""Localized model-price presentation for provider-template configuration.

The provider/model registration remains authoritative in its declared currency.
Settings converts only the visible card/input values to the UI language's
currency, then converts edited input back to the authority currency.  These
specs prevent the dangerous half-change where a currency symbol changes while
the numeric amount (or persisted currency) does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PRESENTATION_TS = (
    ROOT / 'frontend/src/features/settings/model-price-localization.ts')


_HARNESS = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const rates = { USD: 1, CNY: 7.2, JPY: 144, KRW: 1320 };
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="host"></div></body>',
  targets: [process.argv[2]],
  globals: {
    _detectBrand: () => 'generic',
    _brandSvg: () => '',
    _renderProvidersTab: () => {},
    _renderPresetsTab: () => {},
    showAlert: () => {},
    t: (key, values) => key === 'settings.mePriceHint'
      ? `price-hint:${values.currency}` : key,
  },
});

const presentation = global.modelPricePresentation;
window.modelPricePresentation = presentation;

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

  // Bridge the typed presentation owner into the retained Settings adapter,
  // then evaluate the three bounded owners used by the real template editor.
  const indirectEval = eval;
  for (const target of process.argv.slice(4)) {
    indirectEval(fs.readFileSync(target, 'utf8'));
  }
  window.modelPricePresentation = presentation;
  global._i18nLang = window._i18nLang = 'zh';
  global._modelPriceDisplayPolicy = window._modelPriceDisplayPolicy = {
    base_currency: 'USD', usd_rates: rates,
  };
  global._modelPricingCache = window._modelPricingCache = {};
  global._serverConfig = window._serverConfig = {};
  global._stgPresets = window._stgPresets = {};
  const model = {
    model_id: 'priced-model', aliases: [], capabilities: ['text'], rpm: 30,
    pricing: { input: 1, output: 2, currency: 'USD',
      unit: 'per_million_tokens' },
  };
  global._stgProviders = window._stgProviders = [{
    id: 'provider-1', name: 'Provider', models: [model],
  }];

  const host = document.getElementById('host');
  host.innerHTML = _renderModelCard(0, 0, model);
  const cardInputPrice = host.querySelector('.stg-price-val.in').textContent;
  const cardOutputPrice = host.querySelector('.stg-price-val.out').textContent;
  check('zh_card_converts_usd_to_rmb',
    /[¥￥]7(?:\.2)?/.test(cardInputPrice)
    && /[¥￥]14(?:\.4)?/.test(cardOutputPrice));

  _editModel(0, 0);
  let form = host.querySelector('.stg-edit-form');
  check('zh_editor_names_cny',
    form.textContent.indexOf('price-hint:CNY') >= 0);
  check('zh_editor_converts_override_values',
    form.querySelector('.stg-edit-pin').value === '7.2'
    && form.querySelector('.stg-edit-pout').value === '14.4');
  const decoy = document.createElement('div');
  decoy.className = 'stg-edit-form';
  decoy.setAttribute('data-prov', '99');
  decoy.setAttribute('data-model', '99');
  decoy.setAttribute('data-price-authority-currency', 'CNY');
  decoy.setAttribute('data-price-display-currency', 'CNY');
  decoy.innerHTML = '<input class="stg-edit-pin" value="999">' +
    '<input class="stg-edit-pout" value="999">';
  document.body.prepend(decoy);
  _saveModelEdit(0, 0);
  check('scoped_form_preserves_usd_authority_with_decoy_present',
    model.pricing.currency === 'USD'
    && model.pricing.input === 1 && model.pricing.output === 2);

  form.remove();
  _editModel(0, 0);
  form = host.querySelector('.stg-edit-form');
  form.querySelector('.stg-edit-pin').value = '72';
  form.querySelector('.stg-edit-pout').value = '144';
  _saveModelEdit(0, 0);
  check('edited_rmb_round_trips_to_usd_authority',
    model.pricing.currency === 'USD'
    && model.pricing.input === 10 && model.pricing.output === 20);

  // A CNY-authority row identity-round-trips, including the missing-pivot and
  // no-typed-service degraded paths highlighted by independent review.
  form.remove();
  decoy.remove();
  model.pricing = { input: 2, output: 5, currency: 'CNY',
    unit: 'per_million_tokens' };
  host.innerHTML = _renderModelCard(0, 0, model);
  _editModel(0, 0);
  form = host.querySelector('.stg-edit-form');
  check('cny_authority_prefills_cny_in_zh',
    form.querySelector('.stg-edit-pin').value === '2'
    && form.textContent.indexOf('price-hint:CNY') >= 0);
  _saveModelEdit(0, 0);
  check('cny_authority_round_trips_unchanged',
    model.pricing.currency === 'CNY'
    && model.pricing.input === 2 && model.pricing.output === 5);

  form.remove();
  global._modelPriceDisplayPolicy = window._modelPriceDisplayPolicy = {
    base_currency: 'USD', usd_rates: { USD: 1 },
  };
  _editModel(0, 0);
  form = host.querySelector('.stg-edit-form');
  check('missing_cny_pivot_still_identity_prefills',
    form.querySelector('.stg-edit-pin').value === '2'
    && form.textContent.indexOf('price-hint:CNY') >= 0);
  _saveModelEdit(0, 0);
  check('missing_cny_pivot_still_saves_authority',
    model.pricing.currency === 'CNY' && model.pricing.input === 2);

  form.remove();
  window.modelPricePresentation = null;
  _editModel(0, 0);
  form = host.querySelector('.stg-edit-form');
  check('no_service_labels_raw_cny_as_cny',
    form.querySelector('.stg-edit-pin').value === '2'
    && form.textContent.indexOf('price-hint:CNY') >= 0);
  window.modelPricePresentation = presentation;
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
        extra_targets=[
            runtime_section_path('settings.js'),
            runtime_section_path('settings/provider_render.js'),
            runtime_section_path('settings/model_edit.js'),
        ],
        expect_pass=25,
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
