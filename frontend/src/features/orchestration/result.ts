import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';

export interface IssueMessageOptions {
  maxDepth?: number;
  maxMessages?: number;
}

type ResultWindow = Window & {
  orchestrationIssueMessages?: typeof orchestrationIssueMessages;
  orchestrationResultError?: typeof orchestrationResultError;
  orchestrationResultOk?: typeof orchestrationResultOk;
  orchestrationResultData?: typeof orchestrationResultData;
  _orchestrationRequiredResponseFieldsMatch?:
    typeof orchestrationRequiredResponseFieldsMatch;
  _orchestrationContractFieldsMatch?:
    typeof orchestrationContractFieldsMatch;
};

const METADATA_FIELD = /^(ok|status|code|kind|type|title|severity|format|schema|request_?id)$/i;
const ENVELOPE_FIELDS = [
  'error', 'errors', 'detail', 'details',
  'data', 'body', 'envelope', 'inspection', 'validation',
  'diagnostics', 'response', 'outcome',
] as const;

/** Collect bounded, deduplicated human-readable messages from any envelope. */
export function orchestrationIssueMessages(
  value: unknown,
  options: IssueMessageOptions = {},
): string[] {
  const messages: string[] = [];
  const seenMessages = new Set<string>();
  const seenObjects = new WeakSet<object>();
  const maxDepth = options.maxDepth ?? 8;
  const maxMessages = options.maxMessages == null
    ? Number.POSITIVE_INFINITY : Math.max(0, options.maxMessages);

  const add = (item: unknown, label?: string | null): void => {
    if (messages.length >= maxMessages || item == null) return;
    let text = String(item).trim();
    if (!text) return;
    if (label) text = `${String(label).trim()}: ${text}`;
    if (seenMessages.has(text)) return;
    seenMessages.add(text);
    messages.push(text);
  };

  const visit = (item: unknown, depth: number, label?: string | null): void => {
    if (messages.length >= maxMessages || item == null || depth > maxDepth) {
      return;
    }
    if (typeof item === 'string' || typeof item === 'number') {
      add(item, label);
      return;
    }
    if (Array.isArray(item)) {
      for (const entry of item) visit(entry, depth + 1, label);
      return;
    }
    const object = record(item);
    if (!object || seenObjects.has(object)) return;
    seenObjects.add(object);

    let recognized = false;
    if (typeof object.message === 'string'
        || typeof object.message === 'number') {
      visit(object.message, depth + 1, label);
      recognized = true;
    }
    for (const key of ENVELOPE_FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(object, key)
          || object[key] == null) continue;
      visit(object[key], depth + 1, null);
      recognized = true;
    }
    if (recognized) return;

    for (const key of Object.keys(object)) {
      if (METADATA_FIELD.test(key)) continue;
      visit(object[key], depth + 1, key);
    }
  };

  visit(value, 0, null);
  return messages;
}

export function orchestrationResultError(
  value: unknown,
  fallback: unknown,
  options?: IssueMessageOptions,
): string {
  const messages = orchestrationIssueMessages(value, options);
  return messages.length > 0 ? messages.join('; ') : String(fallback || '');
}

export function orchestrationResultOk(value: unknown): boolean {
  if (value === true) return true;
  const outer = record(value);
  if (!outer || outer.ok !== true) return false;
  const data = record(outer.data);
  return !data || data.ok !== false;
}

export function orchestrationResultData(value: unknown): ContractRecord {
  const outer = record(value);
  if (!outer) return {};
  return record(outer.data) ?? outer;
}

export function orchestrationRequiredResponseFieldsMatch(
  body: ContractRecord,
  options: { responseRequiredFields?: readonly string[] } = {},
): boolean {
  const fields = options.responseRequiredFields;
  return !Array.isArray(fields) || fields.length === 0
    || fields.every((field) =>
      Object.prototype.hasOwnProperty.call(body, field));
}

export function orchestrationContractFieldsMatch(
  value: ContractRecord,
  fieldSpecs: unknown,
): boolean {
  const specs = record(fieldSpecs);
  if (!specs) return false;
  const semantics = Object.keys(specs);
  const names: string[] = [];
  const valid = semantics.length > 0 && semantics.every((semantic) => {
    const spec = record(specs[semantic]);
    if (!spec || typeof spec.name !== 'string' || !spec.name
        || ![
          'string', 'boolean', 'nullable_boolean',
          'nullable_non_negative_integer',
        ].includes(String(spec.type))
        || names.includes(spec.name)
        || !Object.prototype.hasOwnProperty.call(value, spec.name)) {
      return false;
    }
    names.push(spec.name);
    const fieldValue = value[spec.name];
    const fieldType = String(spec.type);
    if (fieldType === 'nullable_boolean') {
      return fieldValue === null || typeof fieldValue === 'boolean';
    }
    if (fieldType === 'nullable_non_negative_integer') {
      return fieldValue === null
        || Number.isSafeInteger(fieldValue) && Number(fieldValue) >= 0;
    }
    return typeof fieldValue === fieldType;
  });
  return valid && Object.keys(value).every((name) => names.includes(name));
}

const bridge = orchestrationRegistry as unknown as ResultWindow;
bridge.orchestrationIssueMessages = orchestrationIssueMessages;
bridge.orchestrationResultError = orchestrationResultError;
bridge.orchestrationResultOk = orchestrationResultOk;
bridge.orchestrationResultData = orchestrationResultData;
bridge._orchestrationRequiredResponseFieldsMatch =
  orchestrationRequiredResponseFieldsMatch;
bridge._orchestrationContractFieldsMatch = orchestrationContractFieldsMatch;
