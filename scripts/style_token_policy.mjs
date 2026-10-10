/** Authored CSS admission: token declarations plus exact unchanged legacy copies.
 * Entries: styleTokenViolations, unchangedLegacyTokenCopy.
 * Dependencies: node:crypto and PostCSS; no filesystem, ambient project or publishing.
 */
import { createHash } from 'node:crypto';
import postcss from 'postcss';

const HEX = /#[0-9a-fA-F]{3,8}\b/;
const FUNC_COLOR = /\b(?:rgba|hsla)\(/;
const LAYOUT_PROP = /^(?:(?:margin|padding|inset)(?:-[\w-]+)?|gap|(?:row|column)-gap|top|right|bottom|left)$/;
const RAW_PX = /(?<![\w.-])[+-]?(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)px\b/;

export function unchangedLegacyTokenCopy(text, sha256) {
  if (sha256 === undefined) return false;
  if (typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)) {
    throw new Error('legacyTokenSha256 must be a lowercase SHA-256 digest');
  }
  return createHash('sha256').update(text).digest('hex') === sha256;
}

export function styleTokenViolations(text, relativePath, options = {}) {
  const violations = [], source = postcss.parse(text, { from: relativePath });
  source.walkDecls(declaration => {
    const at = `${relativePath}:${declaration.source.start.line}`;
    const value = declaration.value;
    if (!options.allowColor && HEX.test(value)) violations.push(`${at} hex color: ${declaration.toString()}`);
    if (!options.allowColor && FUNC_COLOR.test(value)) violations.push(`${at} functional color: ${declaration.toString()}`);
    if (LAYOUT_PROP.test(declaration.prop) && RAW_PX.test(value)) {
      violations.push(`${at} raw px in layout property: ${declaration.toString()}`);
    }
  });
  if (options.themeAgnostic) source.walkRules(rule => {
    if (rule.selector.includes('[data-theme')) {
      violations.push(`${relativePath}:${rule.source.start.line} [data-theme] selector in theme-agnostic kit: ${rule.selector}`);
    }
  });
  return violations;
}
