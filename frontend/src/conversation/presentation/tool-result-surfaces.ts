/** Result source and format selection from the shared surface contract.
 * Entry points: resolveToolResultText, toolResultSurfacePolicy. No DOM, storage,
 * transport, second result copy or status inference. Dependencies: generated
 * contract; all raw fields remain on the authoritative Turn projection.
 */
import { TOOL_RESULT_SURFACES } from './tool-result-surfaces.generated';

type RecordValue = Readonly<Record<string, unknown>>;
export type ToolResultText = Readonly<{
  source: 'toolContent' | 'roundResult';
  text: string;
}>;

function record(value: unknown): RecordValue {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : {};
}

function serialized(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined) return 'undefined';
  try { return JSON.stringify(value) ?? String(value); }
  catch { return String(value); }
}

export function resolveToolResultText(value: unknown): ToolResultText | null {
  const root = record(value);
  for (const path of TOOL_RESULT_SURFACES.sources.modelText) {
    let current: RecordValue = root;
    const parts = path.split('.');
    let found = true;
    let result: unknown;
    for (let index = 0; index < parts.length; index += 1) {
      const key = parts[index];
      if (!Object.prototype.hasOwnProperty.call(current, key)) {
        found = false;
        break;
      }
      result = current[key];
      if (index < parts.length - 1) current = record(result);
    }
    if (found) return Object.freeze({
      source: path === 'toolContent' ? 'toolContent' : 'roundResult',
      text: serialized(result),
    });
  }
  return null;
}

export function toolResultSurfacePolicy(toolName: unknown) {
  const name = typeof toolName === 'string' ? toolName : '';
  const policies: Readonly<Record<string, {readonly modelFormat: string;
    readonly frontendFormat: string; readonly reason: string}>> = TOOL_RESULT_SURFACES.toolPolicies;
  return Object.prototype.hasOwnProperty.call(policies, name)
    ? policies[name] : TOOL_RESULT_SURFACES.defaultPolicy;
}
