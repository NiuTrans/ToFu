/* ===== migrated source: settings.js ===== */
// ══════════════════════════════════════════════════════
//  settings.js — Multi-provider settings with nested models
//  Brand SVG paths from LobeHub Icons (MIT License)
//  https://github.com/lobehub/lobe-icons
// ══════════════════════════════════════════════════════

/** Cached server config loaded on first openSettings() */
var _serverConfig = null;

/**
 * Read-only USD-pivot rates supplied by GET /api/v1/server-config.
 * The typed modelPricePresentation service owns conversion/formatting; this
 * retained shell only holds the latest server snapshot for its adapters.
 */
var _modelPriceDisplayPolicy = {
  base_currency: 'USD',
  usd_rates: { USD: 1 },
  updated_at: 0,
  source: 'unavailable',
};

function _modelPricePresentationService() {
  // The typed settings owner registers this service through the private
  // runtime port before any settings action is invoked. Never recreate the
  // deleted classic-script global as a fallback.
  var service = runtimeScope.modelPricePresentation;
  return service && typeof service.displayCurrency === 'function'
    ? service : null;
}

function _modelPriceUsdRates() {
  var rates = _modelPriceDisplayPolicy && _modelPriceDisplayPolicy.usd_rates;
  return rates && typeof rates === 'object' ? rates : { USD: 1 };
}

function _modelPriceUiLanguage() {
  return typeof _i18nLang !== 'undefined' ? _i18nLang : 'en';
}

function _modelPriceDisplayCurrency(authorityCurrency) {
  var source = String(authorityCurrency || 'USD').toUpperCase();
  if (!/^[A-Z]{3}$/.test(source)) source = 'USD';
  var service = _modelPricePresentationService();
  return service
    ? service.displayCurrency(
        _modelPriceUiLanguage(), source, _modelPriceUsdRates())
    : source;
}

function _modelPriceInputForUi(value, sourceCurrency) {
  var service = _modelPricePresentationService();
  if (!service) return value == null ? '' : String(value);
  return service.inputValue(
    value, sourceCurrency || 'USD', _modelPriceUiLanguage(),
    _modelPriceUsdRates());
}

function _modelPriceAuthorityFromUi(value, displayCurrency, authorityCurrency) {
  var service = _modelPricePresentationService();
  if (!service) {
    var number = Number(value);
    return isFinite(number) && number >= 0 ? number : null;
  }
  return service.authorityValue(
    value, displayCurrency, authorityCurrency, _modelPriceUsdRates());
}

/** Cached today's per-key success/failure stats: { day, providers: {pid: {key_name: {...}}} } */
var _keyStatsCache = {
  day: '', providers: {},
  min_attempts: 5, min_success_rate: 0.5,
};
var _keyStatsLoading = false;


/* ═══════════════════════════════════════════════════════════════════
   The body of this file (openSettings, saveSettings, _renderProvidersTab,
   _oauth*, _mcp*, ...) lives in the `frontend/src/runtime/settings/` subpackage.
   The bundler concatenates them in load order (see Vite's module graph)
   so symbols are available in window scope by the time index.html
   wires onclick handlers.
   ═══════════════════════════════════════════════════════════════════ */
