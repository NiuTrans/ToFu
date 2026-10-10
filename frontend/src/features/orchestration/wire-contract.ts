import { orchestrationRegistry } from './registry';
import {
  resolveDirectContractSource,
  type ContractRecord,
  type ContractSource,
} from './contract-source';
import { ORCHESTRATION_WIRE_FORMATS } from './wire-formats.generated';

export interface WireContractSpec {
  contract: ContractRecord | null;
  expected: string;
  actual: string;
  supported: boolean;
}

export interface InspectedWireFormat extends WireContractSpec {
  present: boolean;
  identityField: string;
  compatible: boolean;
}

interface WireIdentity {
  field: string;
  value: string;
  present: boolean;
}

type WireContractWindow = Window & {
  orchestrationWireFormat?: typeof orchestrationWireFormat;
  orchestrationWireContractSpec?: typeof wireContractSpec;
  inspectOrchestrationWireFormat?: typeof inspectWireFormat;
};

export function orchestrationWireFormat(name: string): string {
  return ORCHESTRATION_WIRE_FORMATS[name] || '';
}

function wireIdentity(value: unknown): WireIdentity {
  if (!value || typeof value !== 'object') {
    return { field: '', value: '', present: false };
  }
  const candidate = value as Record<string, unknown>;
  const field = Object.prototype.hasOwnProperty.call(candidate, 'format')
    ? 'format'
    : Object.prototype.hasOwnProperty.call(candidate, 'schema')
      ? 'schema' : '';
  return {
    field,
    value: field && typeof candidate[field] === 'string'
      ? candidate[field] as string : '',
    present: Boolean(field),
  };
}

export function wireContractSpec(
  name: string,
  contractSource?: ContractSource,
): WireContractSpec {
  const expected = orchestrationWireFormat(name);
  if (!expected) {
    const error = new Error(`Unknown orchestration wire contract: ${name}`);
    error.name = 'OrchestrationWireContractError';
    throw error;
  }
  const projected = resolveDirectContractSource(contractSource);
  const identity = wireIdentity(projected);
  const actual = identity.value;
  return {
    contract: projected,
    expected,
    actual,
    supported: !actual || actual === expected,
  };
}

export function inspectWireFormat(
  name: string,
  payload: unknown,
  contractSource?: ContractSource,
): InspectedWireFormat {
  const spec = wireContractSpec(name, contractSource);
  const identity = wireIdentity(payload);
  return {
    contract: spec.contract,
    expected: spec.expected,
    actual: identity.value,
    present: identity.present,
    identityField: identity.field,
    compatible: !identity.present,
    supported: spec.supported && (
      !identity.present || identity.value === spec.expected),
  };
}

const bridge = orchestrationRegistry as unknown as WireContractWindow;
bridge.orchestrationWireFormat = orchestrationWireFormat;
bridge.orchestrationWireContractSpec = wireContractSpec;
bridge.inspectOrchestrationWireFormat = inspectWireFormat;
