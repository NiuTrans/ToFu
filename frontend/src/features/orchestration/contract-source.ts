export type ContractRecord = Record<string, unknown>;
export type ContractSource = unknown | (() => unknown);

export function record(value: unknown): ContractRecord | null {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as ContractRecord : null;
}

export function resolveDirectContractSource(
  source?: ContractSource,
): ContractRecord | null {
  let contract = source;
  if (typeof source === 'function') {
    try {
      contract = source();
    } catch {
      contract = null;
    }
  }
  return record(contract);
}

export function resolveContractSource(
  name: string,
  source?: ContractSource,
): ContractRecord | null {
  const wrapper = resolveDirectContractSource(source);
  if (!wrapper) return null;
  return Object.prototype.hasOwnProperty.call(wrapper, name)
    ? record(wrapper[name]) : wrapper;
}
