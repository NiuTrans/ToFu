import { orchestrationRegistry } from './registry';
import { record, wireContractSpec, type ContractRecord } from './contracts';
import {
  orchestrationActionRead,
  orchestrationActionReason,
  orchestrationRequiredResponseFieldsMatch,
  registerOrchestrationHttpReadProjectors,
  type OrchestrationReadOptions,
} from './read-core';
import {
  orchestrationDurableRunMatches,
  projectOrchestrationDurableRunSnapshot,
} from './durable-run-snapshot';
import { orchestrationResultError } from './result';
import { normalizeOrchestrationRuntimeStart } from './runtime-read';

type DurableReadWindow = Window & {
  normalizeOrchestrationTaskRead?: typeof normalizeOrchestrationTaskRead;
  normalizeOrchestrationTaskCreate?: typeof normalizeOrchestrationTaskCreate;
};

export function normalizeOrchestrationTaskRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const wire = wireContractSpec('durable-run', options.durableRunContract);
  const rawRun = read.recognized && body.ok === true && wire.supported
    && orchestrationRequiredResponseFieldsMatch(body, options)
    && record(body.run)
    && orchestrationDurableRunMatches(
      body.run,
      'readFields',
      wire.contract,
      options.runContract,
      Array.isArray(options.requestArgs) ? options.requestArgs[0] : '',
    ) ? body.run as ContractRecord : null;
  const accepted = Boolean(rawRun);
  const run = accepted
    ? projectOrchestrationDurableRunSnapshot(rawRun, wire.contract) : null;
  let reason = !wire.supported ? 'unsupported-format'
    : orchestrationActionReason(read, accepted, 'read-rejected');
  if (!accepted && read.status === 404) reason = 'not-found';
  return {
    ok: accepted,
    status: read.status,
    reason,
    notFound: reason === 'not-found',
    run,
    runId: run ? run.id : '',
    expectedFormat: wire.expected,
    contractFormat: wire.actual,
    unsupportedFormat: !wire.supported,
    response: value,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationTaskCreate(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const start = normalizeOrchestrationRuntimeStart(
    value, 'durable', 'create-rejected', options);
  return {
    ok: start.ok,
    status: start.status,
    reason: start.reason,
    runId: start.runtimeId,
    failedRunId: start.failedRuntimeId,
    identity: start.identity,
    canonical: start.canonical,
    expectedFormat: start.expectedFormat,
    wireFormat: start.wireFormat,
    data: start.data || {},
    response: value,
    error: start.error,
  };
}

registerOrchestrationHttpReadProjectors({
  'task-read': normalizeOrchestrationTaskRead,
  'task-create': normalizeOrchestrationTaskCreate,
});

Object.assign(orchestrationRegistry as unknown as DurableReadWindow, {
  normalizeOrchestrationTaskRead,
  normalizeOrchestrationTaskCreate,
});
