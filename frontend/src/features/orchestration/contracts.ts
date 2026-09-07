// Stable facade retained for consumers while source resolution, rolling
// defaults and versioned wire policy live in focused physical owners.
export {
  record,
  resolveContractSource,
  resolveDirectContractSource,
  type ContractRecord,
  type ContractSource,
} from './contract-source';
export {
  compatibilityContract,
  publishedContract,
} from './compatibility-contracts';
export {
  inspectWireFormat,
  orchestrationWireFormat,
  wireContractSpec,
  type InspectedWireFormat,
  type WireContractSpec,
} from './wire-contract';
