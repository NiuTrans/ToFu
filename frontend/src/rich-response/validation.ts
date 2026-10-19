/** Admission boundary: schema-derived, closed, complete JSON; never evaluates payload code. */
import { richResponseSchema, type RichResponse } from './contract.generated';

import { safeCitationUrl } from './citations';
type Schema = { $ref?: string; const?: unknown; enum?: readonly unknown[]; oneOf?: readonly Schema[]; type?: string; properties?: Record<string, Schema>; required?: readonly string[]; additionalProperties?: boolean; items?: Schema; minItems?: number; maxItems?: number; minLength?: number; maxLength?: number; pattern?: string; minimum?: number; maximum?: number; exclusiveMinimum?: number };
function matches(value: unknown, schema: Schema, depth = 0): boolean {
  if (depth > 16) return false;
  if (schema.$ref) return matches(value, (richResponseSchema.$defs as Record<string, Schema>)[schema.$ref.split('/').at(-1)!], depth + 1);
  if ('const' in schema && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  if (schema.oneOf) return schema.oneOf.filter(s => matches(value, s, depth + 1)).length === 1;
  if (schema.type === 'object') {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const record = value as Record<string, unknown>;
    const properties = schema.properties ?? {};
    return (schema.required ?? []).every(k => Object.hasOwn(record, k))
      && Object.keys(record).every(k => Object.hasOwn(properties, k) && matches(record[k], properties[k], depth + 1));
  }
  if (schema.type === 'array') return Array.isArray(value) && value.length >= (schema.minItems ?? 0) && value.length <= (schema.maxItems ?? Infinity) && value.every(v => matches(v, schema.items!, depth + 1));
  if (schema.type === 'string') return typeof value === 'string' && [...value].length >= (schema.minLength ?? 0) && [...value].length <= (schema.maxLength ?? 1000) && (!schema.pattern || new RegExp(schema.pattern).test(value));
  if (schema.type === 'number' || schema.type === 'integer') return typeof value === 'number' && Number.isFinite(value)
    && (schema.type !== 'integer' || Number.isInteger(value))
    && value > (schema.exclusiveMinimum ?? -Infinity) && value >= (schema.minimum ?? -Infinity) && value <= (schema.maximum ?? Infinity);
  if (schema.type === 'boolean') return typeof value === 'boolean';
  if (schema.type === 'null') return value === null;
  return schema.type === undefined && ('const' in schema || !!schema.enum);
}
export function validateRichResponse(value: unknown): RichResponse | null {
  if (!matches(value, richResponseSchema as Schema)) return null;
  const response = value as RichResponse;
  // Keys must be unambiguous for lifecycle reconciliation.
  if (new Set(response.blocks.map(b => b.id)).size !== response.blocks.length) return null;
  if (response.blocks.some(block => block.type === 'table'
    && block.rows.some(row => row.length !== block.columns.length))) return null;
  if (response.layout) {
    const groups = response.layout.groups;
    const ids = groups.flatMap(group => group.blocks);
    if (!groups.length || new Set(groups.map(group => group.id)).size !== groups.length
      || groups.some(group => !group.blocks.length) || new Set(ids).size !== ids.length
      || ids.length !== response.blocks.length || ids.some(id => !response.blocks.some(block => block.id === id))) return null;
  }
  for (const block of response.blocks) {
    if (block.type === 'metric_grid' && (!block.items.length || new Set(block.items.map(item => item.id)).size !== block.items.length)) return null;
    if (block.type === 'workflow' && (!block.steps.length || new Set(block.steps.map(step => step.id)).size !== block.steps.length)) return null;
    if (block.type === 'resource_panel' && block.snapshot) {
      const { rows, fetchedAt } = block.snapshot;
      if (!/(Z|[+-]\d{2}:\d{2})$/.test(fetchedAt) || !Number.isFinite(Date.parse(fetchedAt))
        || new Set(rows.map(row => row.id)).size !== rows.length
        || rows.some(row => [row.total, row.allocated, row.available, row.pending].some(value => value != null && value < 0))) return null;
    }

    if (block.type === 'citations' && (new Set(block.sources.map(source => source.id)).size !== block.sources.length
      || block.sources.some(source => !safeCitationUrl(source.url)))) return null;

    if (block.type === 'data_chart') {
      if (new Set(block.series.map(series => series.id)).size !== block.series.length) return null;
      const points = block.series.flatMap(series => series.points);
      if ((block.kind === 'line' || block.kind === 'scatter') && points.some(point => point.x === undefined)) return null;
      if ((block.kind === 'bar' || block.kind === 'pie') && points.some(point => !point.label)) return null;
      if (block.kind === 'pie' && (block.series.length !== 1 || points.some(point => point.value < 0)
        || !(points.reduce((total, point) => total + point.value, 0) > 0))) return null;
    }
    if (block.type !== 'timeline') continue;
    const { start, end } = block.axis;
    const laneIds = new Set(block.lanes.map(lane => lane.id));
    if (!(start < end) || laneIds.size !== block.lanes.length) return null;
    if (block.lanes.some(lane => lane.intervals.some(interval =>
      !(start <= interval.start && interval.start < interval.end && interval.end <= end)))) return null;
    if (block.markers.some(marker => marker.time < start || marker.time > end
      || (marker.laneId !== undefined && !laneIds.has(marker.laneId)))) return null;
  }
  return response;
}
export function parseRichResponse(source: string): RichResponse | null {
  try { return validateRichResponse(JSON.parse(source)); } catch { return null; }
}
export type RichPart = { kind: 'markdown' | 'rich' | 'partial'; source: string };
/** Only top-level complete fences activate. Nested fences remain literal Markdown. */
export function splitRichMarkdown(source: string): RichPart[] {

  const parts: RichPart[] = [];
  const lines = source.split(/(?<=\n)/);
  let markdown = '', fence = '', rich = false, payload = '', opening = '';
  for (const line of lines) {
    const open = /^( {0,3})(`{3,}|~{3,})([^\r\n]*)\r?\n?$/.exec(line);
    if (!fence && open) {
      fence = open[2]; rich = open[3].trim() === 'tofu-ui';
      if (rich) { if (markdown) parts.push({kind:'markdown',source:markdown}); markdown = ''; opening = line; payload = ''; }
      else markdown += line;
    } else if (fence && new RegExp(`^ {0,3}${fence[0]}{${fence.length},}\\s*$`).test(line)) {
      if (rich) parts.push({kind:'rich',source:payload}); else markdown += line;
      fence = ''; rich = false;
    } else if (rich) payload += line;
    else markdown += line;
  }
  if (rich) parts.push({kind:'partial',source:opening + payload});
  if (markdown) parts.push({kind:'markdown',source:markdown});
  return parts;
}
