import { orchestrationRegistry } from './registry';
import {
  ORCHESTRATION_HTTP_READ_PROJECTORS,
  type OrchestrationHttpReadProjector,
  type OrchestrationReadOptions,
} from './read-core';

type HttpReadWindow = Window & {
  orchestrationHttpReadProjector?: typeof orchestrationHttpReadProjector;
  projectOrchestrationHttpRead?: typeof projectOrchestrationHttpRead;
};

export function orchestrationHttpReadProjector(
  name: string,
): OrchestrationHttpReadProjector | null {
  return ORCHESTRATION_HTTP_READ_PROJECTORS[name] ?? null;
}

export function projectOrchestrationHttpRead(
  options: OrchestrationReadOptions | null | undefined,
  optionName: string,
  responseContract: string,
  value: unknown,
  context?: unknown,
): unknown {
  const configured = options ?? {};
  const override = configured[optionName];
  const projector = typeof override === 'function'
    ? override as OrchestrationHttpReadProjector
    : orchestrationHttpReadProjector(responseContract);
  if (typeof projector !== 'function') {
    const error = new Error(
      `Unknown orchestration response contract: ${responseContract}`);
    error.name = 'OrchestrationHttpReadProjectorError';
    throw error;
  }
  const projectorOptions = context && typeof context === 'object'
    ? Object.assign({}, configured, context) as OrchestrationReadOptions
    : configured;
  return projector(value, projectorOptions);
}

Object.assign(orchestrationRegistry as unknown as HttpReadWindow, {
  orchestrationHttpReadProjector,
  projectOrchestrationHttpRead,
});
