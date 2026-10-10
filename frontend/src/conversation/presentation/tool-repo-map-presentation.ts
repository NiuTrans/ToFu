/**
 * Repo-map identity and requested scope over immutable projected tool rounds.
 * Entry point: renderRepoMapHeaderHtml; used by live and settled headers.
 * Dependencies: shared escaping and translator. No DOM, defaults, result parsing,
 * state or execution claims; malformed history keeps its bounded original title.
 */
import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';

type RecordValue = Readonly<Record<string, unknown>>;
export const REPO_MAP_HEADER_LIMITS = Object.freeze({
  argumentUnits: 32_768,
  valueUnits: 512,
});

function record(value: unknown): RecordValue | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : null;
}
function own(value: RecordValue, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(value, key) ? value[key] : undefined;
}
function bounded(value: string): string {
  const limit = REPO_MAP_HEADER_LIMITS.valueUnits;
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}
function argumentsRecord(raw: unknown): RecordValue | null {
  if (typeof raw !== 'string') return record(raw);
  if (raw.length > REPO_MAP_HEADER_LIMITS.argumentUnits) return null;
  try { return record(JSON.parse(raw) as unknown); } catch { return null; }
}

/** Protocol parameter names stay exact; missing fields do not invent defaults. */
export function renderRepoMapHeaderHtml(roundValue: unknown, translate: Translator): string {
  const round = record(roundValue);
  if (!round || own(round, 'toolName') !== 'repo_map') return '';
  const label = translate('tool.repoMap.label');
  const title = `${escapeHtmlText(label)}<span class="ptool-tool-hint"> · ${
    escapeHtmlText(translate('tool.repoMap.description'))
  }</span>`;
  const args = argumentsRecord(own(round, 'toolArgs'));
  const parameters: string[] = [];
  if (args) {
    for (const key of ['path', 'focus', 'max_tokens']) {
      const value = own(args, key);
      const shown = key === 'max_tokens'
        ? typeof value === 'number' && Number.isSafeInteger(value) && value > 0
          ? String(value) : null
        : typeof value === 'string' ? value === '' ? '""' : bounded(value) : null;
      if (shown !== null) {
        parameters.push(`<span class="ptool-repo-map-param">${key}: <code>${escapeHtmlText(shown)}</code></span>`);
      }
    }
  } else {
    const query = own(round, 'query');
    // Sparse historical rows may retain a useful scope only in their old title.
    if (typeof query === 'string' && query.trim()
      && query !== 'repo_map' && query !== 'Repo Map' && query !== label) {
      parameters.push(escapeHtmlText(bounded(query)));
    }
  }
  return title + (parameters.length
    ? `<br><span class="ptool-repo-map-params">${parameters.join(' · ')}</span>` : '');
}
