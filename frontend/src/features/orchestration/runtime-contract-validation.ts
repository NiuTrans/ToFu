import { orchestrationRegistry } from './registry';
import {
  type RuntimeSectionValidator,
} from './contract-validation-primitives';
import { validateDurableRunRuntimeSection } from './durable-list-contract-validation';
import { validateEventRuntimeSection } from './event-contract-validation';
import { validateMutationRuntimeSection } from './mutation-contract-validation';
import { validateOutcomeRuntimeSection } from './outcome-contract-validation';
import { validateReplayRuntimeSection } from './replay-contract-validation';
import { validateRequestLimitsRuntimeSection } from './request-limit-contract-validation';
import { validateRunRuntimeSection } from './run-contract-validation';
import { validateRuntimeStartRuntimeSection } from './runtime-start-contract-validation';
import { validateTraceRuntimeSection } from './trace-contract-validation';

type RuntimeValidationWindow = Window & {
  ORCHESTRATION_RUNTIME_SECTION_VALIDATORS?: Readonly<
    Record<string, RuntimeSectionValidator>
  >;
  _validateNodeRuntimeDefaultsAuthoringSection?: RuntimeSectionValidator;
};

const validateNodeRuntimeDefaults: RuntimeSectionValidator = (
  section,
  missing,
) => {
  const privateValidator = (
    orchestrationRegistry as unknown as RuntimeValidationWindow
  )._validateNodeRuntimeDefaultsAuthoringSection;
  const globalValidator = (
    globalThis as unknown as RuntimeValidationWindow
  )._validateNodeRuntimeDefaultsAuthoringSection;
  const validator = typeof privateValidator === 'function'
    ? privateValidator
    : globalValidator;
  if (typeof validator === 'function') validator(section, missing);
  else missing.push('nodeRuntimeDefaults');
};

export const ORCHESTRATION_RUNTIME_SECTION_VALIDATORS = Object.freeze({
  outcomeContract: validateOutcomeRuntimeSection,
  eventContract: validateEventRuntimeSection,
  runContract: validateRunRuntimeSection,
  traceContract: validateTraceRuntimeSection,
  mutationContract: validateMutationRuntimeSection,
  replayContract: validateReplayRuntimeSection,
  runtimeStartContract: validateRuntimeStartRuntimeSection,
  durableRunContract: validateDurableRunRuntimeSection,
  requestLimits: validateRequestLimitsRuntimeSection,
  nodeRuntimeDefaults: validateNodeRuntimeDefaults,
});

(orchestrationRegistry as unknown as RuntimeValidationWindow).ORCHESTRATION_RUNTIME_SECTION_VALIDATORS =
  ORCHESTRATION_RUNTIME_SECTION_VALIDATORS;
