import { orchestrationRegistry } from './registry';
export type MissingPaths = string[];
export type ValidationRecord = Record<string, unknown>;
export type RuntimeSectionValidator = (
  section: ValidationRecord,
  missing: MissingPaths,
) => void;

type PrimitiveWindow = Window & {
  _orchestrationContractRecord?: typeof orchestrationContractRecord;
  _orchestrationRequireArray?: typeof orchestrationRequireArray;
  _orchestrationRequireArraySubset?: typeof orchestrationRequireArraySubset;
  _orchestrationRequireString?: typeof orchestrationRequireString;
  _orchestrationRequireStringFields?: typeof orchestrationRequireStringFields;
  _orchestrationRequireMapValues?: typeof orchestrationRequireMapValues;
  _orchestrationRequirePositiveInteger?: typeof orchestrationRequirePositiveInteger;
  _orchestrationRequireBoolean?: typeof orchestrationRequireBoolean;
  _orchestrationRequireOptional?: typeof orchestrationRequireOptional;
  _orchestrationRequireStringVocabulary?: typeof orchestrationRequireStringVocabulary;
  _orchestrationRequireFieldSpecs?: typeof orchestrationRequireFieldSpecs;
};

export function orchestrationContractRecord(
  value: unknown,
): value is ValidationRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function orchestrationRequireArray(
  value: unknown,
  path: string,
  missing: MissingPaths,
  requiredValues: readonly unknown[] | null | undefined,
): void {
  if (!Array.isArray(value)
      || (requiredValues ?? []).some((required) => !value.includes(required))) {
    missing.push(path);
  }
}

export function orchestrationRequireArraySubset(
  value: unknown,
  allowed: unknown,
  path: string,
  missing: MissingPaths,
): boolean {
  const valid = Array.isArray(value) && Array.isArray(allowed)
    && value.every((item) => allowed.includes(item));
  if (!valid) missing.push(path);
  return valid;
}

export function orchestrationRequireString(
  value: unknown,
  path: string,
  missing: MissingPaths,
): void {
  if (typeof value !== 'string' || !value) missing.push(path);
}

export function orchestrationRequireStringFields(
  value: ValidationRecord,
  fields: readonly string[],
  path: string,
  missing: MissingPaths,
): void {
  fields.forEach((field) => orchestrationRequireString(
    value[field], `${path}.${field}`, missing));
}

export function orchestrationRequireMapValues(
  value: unknown,
  fields: readonly string[],
  allowedValues: readonly unknown[],
  path: string,
  missing: MissingPaths,
): void {
  const candidate = orchestrationContractRecord(value) ? value : {};
  fields.forEach((field) => {
    if (!allowedValues.includes(candidate[field])) {
      missing.push(`${path}.${field}`);
    }
  });
}

export function orchestrationRequirePositiveInteger(
  value: unknown,
  path: string,
  missing: MissingPaths,
): void {
  if (!Number.isSafeInteger(value) || Number(value) <= 0) missing.push(path);
}

export function orchestrationRequireBoolean(
  value: unknown,
  path: string,
  missing: MissingPaths,
): void {
  if (typeof value !== 'boolean') missing.push(path);
}

export function orchestrationRequireOptional(
  value: unknown,
  path: string,
  missing: MissingPaths,
  accepts: (candidate: unknown) => boolean,
): void {
  if (value != null && !accepts(value)) missing.push(path);
}

export function orchestrationRequireStringVocabulary(
  value: unknown,
  path: string,
  missing: MissingPaths,
): value is string[] {
  if (!Array.isArray(value) || !value.length) {
    missing.push(path);
    return false;
  }
  const seen = new Set<string>();
  const valid = value.every((entry) => {
    if (typeof entry !== 'string' || !entry || seen.has(entry)) return false;
    seen.add(entry);
    return true;
  });
  if (!valid) missing.push(path);
  return valid;
}

export function orchestrationRequireFieldSpecs(
  value: unknown,
  expectedTypes: Readonly<Record<string, string>>,
  path: string,
  missing: MissingPaths,
): boolean {
  const fields = orchestrationContractRecord(value) ? value : {};
  const semantics = Object.keys(expectedTypes);
  const names = new Set<string>();
  const valid = Object.keys(fields).length === semantics.length
    && semantics.every((semantic) => {
      const spec = orchestrationContractRecord(fields[semantic])
        ? fields[semantic] : {};
      const name = spec.name;
      if (Object.keys(spec).length !== 2 || typeof name !== 'string' || !name
          || spec.type !== expectedTypes[semantic] || names.has(name)) {
        return false;
      }
      names.add(name);
      return true;
    });
  if (!valid) missing.push(path);
  return valid;
}

Object.assign(orchestrationRegistry as unknown as PrimitiveWindow, {
  _orchestrationContractRecord: orchestrationContractRecord,
  _orchestrationRequireArray: orchestrationRequireArray,
  _orchestrationRequireArraySubset: orchestrationRequireArraySubset,
  _orchestrationRequireString: orchestrationRequireString,
  _orchestrationRequireStringFields: orchestrationRequireStringFields,
  _orchestrationRequireMapValues: orchestrationRequireMapValues,
  _orchestrationRequirePositiveInteger: orchestrationRequirePositiveInteger,
  _orchestrationRequireBoolean: orchestrationRequireBoolean,
  _orchestrationRequireOptional: orchestrationRequireOptional,
  _orchestrationRequireStringVocabulary: orchestrationRequireStringVocabulary,
  _orchestrationRequireFieldSpecs: orchestrationRequireFieldSpecs,
});
