import { orchestrationRegistry } from './registry';
import { compatibilityContract, record, type ContractRecord } from './contracts';
import { orchestrationDefinitionVersion } from './definition-write-result';

type DefinitionResponseWindow = Window & {
  _orchestrationDefinitionFields?: typeof orchestrationDefinitionFields;
  _orchestrationDefinitionVersionMatches?:
    typeof orchestrationDefinitionVersionMatches;
  _orchestrationDefinitionEntryMatches?:
    typeof orchestrationDefinitionEntryMatches;
  _orchestrationDefinitionListMatches?:
    typeof orchestrationDefinitionListMatches;
};

export function orchestrationDefinitionFields(
  value: ContractRecord,
  fields: unknown,
): ContractRecord {
  if (!Array.isArray(fields) || !fields.length) return value;
  const projected: ContractRecord = {};
  fields.forEach((field) => {
    const name = String(field);
    if (Object.prototype.hasOwnProperty.call(value, name)) {
      projected[name] = value[name];
    }
  });
  return projected;
}

export function orchestrationDefinitionVersionMatches(
  value: unknown,
  nullable: boolean,
): boolean {
  return (nullable && value === null)
    || orchestrationDefinitionVersion(value) !== null;
}

export function orchestrationDefinitionEntryMatches(
  value: unknown,
  source: unknown,
  expectedId?: unknown,
): boolean {
  const entry = record(value);
  if (!entry) return false;
  const contract = record(source) ?? {};
  const defaults = compatibilityContract('definitionEntryContract') ?? {};
  const fields = Array.isArray(contract.fields)
    ? contract.fields : Array.isArray(defaults.fields) ? defaults.fields : [];
  if (!fields.every((field) => typeof field === 'string'
      && Object.prototype.hasOwnProperty.call(entry, field))) return false;
  if (typeof entry.id !== 'string' || !entry.id
      || (expectedId && entry.id !== String(expectedId))
      || typeof entry.name !== 'string' || !entry.name
      || !record(entry.definition)) return false;
  const versionField = String(
    contract.versionField || defaults.versionField);
  return orchestrationDefinitionVersionMatches(entry[versionField], false)
    && (!Object.prototype.hasOwnProperty.call(entry, 'createdAt')
      || orchestrationDefinitionVersionMatches(entry.createdAt, false));
}

export function orchestrationDefinitionListMatches(
  items: unknown[],
  source: unknown,
  versionField: string,
): boolean {
  const contract = record(source) ?? {};
  const defaults = compatibilityContract('definitionListContract') ?? {};
  const fields = Array.isArray(contract.itemFields)
    ? contract.itemFields
    : Array.isArray(defaults.itemFields) ? defaults.itemFields : [];
  const seen = new Set<string>();
  return items.every((item) => {
    const entry = record(item);
    if (!entry
        || !fields.every((field) => typeof field === 'string'
          && Object.prototype.hasOwnProperty.call(entry, field))
        || typeof entry.id !== 'string' || !entry.id || seen.has(entry.id)
        || typeof entry.name !== 'string') return false;
    seen.add(entry.id);
    if (Object.prototype.hasOwnProperty.call(entry, 'nodeCount')
        && (!Number.isSafeInteger(entry.nodeCount)
          || Number(entry.nodeCount) < 0)) return false;
    return !Object.prototype.hasOwnProperty.call(entry, versionField)
      || orchestrationDefinitionVersionMatches(entry[versionField], true);
  });
}

Object.assign(orchestrationRegistry as unknown as DefinitionResponseWindow, {
  _orchestrationDefinitionFields: orchestrationDefinitionFields,
  _orchestrationDefinitionVersionMatches: orchestrationDefinitionVersionMatches,
  _orchestrationDefinitionEntryMatches: orchestrationDefinitionEntryMatches,
  _orchestrationDefinitionListMatches: orchestrationDefinitionListMatches,
});
