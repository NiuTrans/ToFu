/**
 * Settings model-price localization.
 *
 * Responsibility: project one canonical per-million-token price into the
 * currency implied by the active UI language, and convert edited display
 * values back to the canonical row's declared currency. It never owns model
 * prices, exchange-rate refresh, persistence, or billing arithmetic.
 */

export type ModelPriceCurrency = 'USD' | 'CNY' | 'JPY' | 'KRW';
export type UsdRateMap = Readonly<Record<string, unknown>>;

const LANGUAGE_CURRENCY: Readonly<Record<string, ModelPriceCurrency>> = Object.freeze({
  zh: 'CNY',
  ja: 'JPY',
  ko: 'KRW',
  en: 'USD',
});

const LANGUAGE_LOCALE: Readonly<Record<string, string>> = Object.freeze({
  zh: 'zh-CN',
  ja: 'ja-JP',
  ko: 'ko-KR',
  en: 'en-US',
});

function languageRoot(language: unknown): string {
  return String(language ?? '').trim().toLowerCase().split(/[-_]/, 1)[0] || 'en';
}

function currencyCode(currency: unknown): string {
  const code = String(currency ?? '').trim().toUpperCase();
  return /^[A-Z]{3}$/.test(code) ? code : 'USD';
}

function priceNumber(value: unknown): number | null {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function usdRate(currency: string, rates: UsdRateMap): number | null {
  if (currency === 'USD') return 1;
  const raw = rates?.[currency];
  const rate = typeof raw === 'number' ? raw : Number(raw);
  return Number.isFinite(rate) && rate > 0 ? rate : null;
}

/** Map a UI language (including regional tags) to its preferred currency. */
export function currencyForLanguage(language: unknown): ModelPriceCurrency {
  return LANGUAGE_CURRENCY[languageRoot(language)] ?? 'USD';
}

/**
 * Select an executable display currency for one authority row. If either USD
 * pivot is absent, identity-display the authority currency: the field remains
 * editable and can never acquire a symbol for an amount that was not converted.
 */
export function displayCurrencyForUi(
  language: unknown,
  authorityCurrency: unknown,
  rates: UsdRateMap,
): string {
  const authority = currencyCode(authorityCurrency);
  const wanted = currencyForLanguage(language);
  if (wanted === authority) return authority;
  if (usdRate(authority, rates) !== null && usdRate(wanted, rates) !== null) {
    return wanted;
  }
  return authority;
}

/**
 * Convert through the policy's USD pivot. Missing/invalid axes return null;
 * callers must not relabel an unconverted amount with the target currency.
 */
export function convertModelPrice(
  value: unknown,
  sourceCurrency: unknown,
  targetCurrency: unknown,
  rates: UsdRateMap,
): number | null {
  const amount = priceNumber(value);
  if (amount === null) return null;
  const source = currencyCode(sourceCurrency);
  const target = currencyCode(targetCurrency);
  if (source === target) return amount;
  const sourceRate = usdRate(source, rates);
  const targetRate = usdRate(target, rates);
  if (sourceRate === null || targetRate === null) return null;
  return (amount / sourceRate) * targetRate;
}

function editableDecimal(value: number): string {
  if (value === 0) return '0';
  const places = Math.abs(value) >= 100 ? 4 : Math.abs(value) >= 1 ? 6 : 10;
  return value.toFixed(places).replace(/(?:\.0+|(\.\d*?)0+)$/, '$1');
}

/** Numeric input value in the currency implied by the UI language. */
export function modelPriceInputValue(
  value: unknown,
  sourceCurrency: unknown,
  language: unknown,
  rates: UsdRateMap,
): string {
  const converted = convertModelPrice(
    value, sourceCurrency,
    displayCurrencyForUi(language, sourceCurrency, rates), rates);
  return converted === null ? '' : editableDecimal(converted);
}

/** Convert an editor value back to the model row's authority currency. */
export function modelPriceAuthorityValue(
  displayValue: unknown,
  displayCurrency: unknown,
  authorityCurrency: unknown,
  rates: UsdRateMap,
): number | null {
  const converted = convertModelPrice(
    displayValue, displayCurrency, authorityCurrency, rates);
  if (converted === null) return null;
  // Bound floating-point tails while preserving sub-cent model rates.
  return Number(converted.toPrecision(12));
}

/** Compact localized currency text for a provider model card. */
export function formatModelPriceForUi(
  value: unknown,
  sourceCurrency: unknown,
  language: unknown,
  rates: UsdRateMap,
): string {
  const amount = priceNumber(value);
  if (amount === null) return '—';
  const root = languageRoot(language);
  const wantedCurrency = displayCurrencyForUi(
    root, sourceCurrency, rates);
  const converted = convertModelPrice(
    amount, sourceCurrency, wantedCurrency, rates);
  // Fail closed: if a pivot is unavailable, keep both the source amount and
  // source currency instead of attaching the requested symbol to USD digits.
  const displayCurrency = converted === null
    ? currencyCode(sourceCurrency) : wantedCurrency;
  const displayAmount = converted === null ? amount : converted;
  const absolute = Math.abs(displayAmount);
  const isWholeUnitCurrency = displayCurrency === 'JPY' || displayCurrency === 'KRW';
  const maximumFractionDigits = isWholeUnitCurrency && absolute >= 1
    ? 0 : absolute >= 1 ? 2 : 4;
  try {
    return new Intl.NumberFormat(LANGUAGE_LOCALE[root] ?? 'en-US', {
      style: 'currency',
      currency: displayCurrency,
      currencyDisplay: 'narrowSymbol',
      minimumFractionDigits: 0,
      maximumFractionDigits,
    }).format(displayAmount);
  } catch {
    return `${displayCurrency} ${editableDecimal(displayAmount)}`;
  }
}

/** One immutable port injected into the retained Settings renderer. */
export const modelPricePresentation = Object.freeze({
  currencyForLanguage,
  displayCurrency: displayCurrencyForUi,
  convert: convertModelPrice,
  inputValue: modelPriceInputValue,
  authorityValue: modelPriceAuthorityValue,
  formatForUi: formatModelPriceForUi,
});
