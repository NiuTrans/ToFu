/**
 * Shared HTML string-safety primitives.
 *
 * Responsibility: escape untrusted text and assemble explicitly trusted HTML
 * templates without reading DOM, browser globals, or retained runtime state.
 * Entry points: `escapeHtml`, `escapeHtmlText`, `safeHtml`, and the explicit
 * `raw` escape hatch. Dependencies: none.
 */

const HTML_ESCAPE_ENTITIES: Readonly<Record<string, string>> = Object.freeze({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
});
const HTML_ESCAPE_PATTERN = /[&<>"']/g;

/**
 * Escape text for an HTML text or quoted-attribute context.
 *
 * Direct calls preserve the retained runtime's falsy-input contract: null,
 * undefined, false, zero, and the empty string all produce an empty string.
 */
export function escapeHtml(value: unknown): string {
  if (!value) return '';
  const text = typeof value === 'string' ? value : String(value);
  return text.replace(
    HTML_ESCAPE_PATTERN,
    (character) => HTML_ESCAPE_ENTITIES[character],
  );
}

/** Escape a display value while preserving meaningful false and zero text. */
export function escapeHtmlText(value: unknown): string {
  return escapeHtml(String(value ?? ''));
}

/** String-like result whose markup has passed through this module's policy. */
export interface SafeHtmlOutput {
  readonly value: string;
  toString(): string;
}

/**
 * Runtime-only brand for trusted markup. A class-backed `instanceof` check
 * prevents a JSON-shaped object from impersonating an approved value.
 */
class SafeHtmlValue implements SafeHtmlOutput {
  readonly value: string;

  constructor(value: unknown) {
    this.value = value == null ? '' : String(value);
  }

  toString(): string {
    return this.value;
  }
}

/** Mark sanitized or hardcoded markup for intentional unescaped insertion. */
export function raw(value: unknown): SafeHtmlOutput {
  return new SafeHtmlValue(value);
}

function safeHtmlPart(value: unknown): string {
  if (value == null) return '';
  if (value instanceof SafeHtmlValue) return value.value;
  if (Array.isArray(value)) {
    let output = '';
    for (const item of value) output += safeHtmlPart(item);
    return output;
  }
  // Coerce before calling escapeHtml so 0 and false remain visible inside a
  // template even though direct escapeHtml calls preserve their old contract.
  return escapeHtmlText(value);
}

/**
 * Assemble HTML while escaping every interpolation by default. Nested
 * `safeHtml` results and values explicitly wrapped with `raw` compose without
 * double escaping; arrays recursively follow the same rules.
 */
export function safeHtml(
  strings: TemplateStringsArray,
  ...values: readonly unknown[]
): SafeHtmlOutput {
  let output = strings[0];
  for (let index = 0; index < values.length; index += 1) {
    output += safeHtmlPart(values[index]) + strings[index + 1];
  }
  return new SafeHtmlValue(output);
}
