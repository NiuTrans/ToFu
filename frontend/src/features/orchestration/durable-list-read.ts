import { orchestrationRegistry } from './registry';
import { compatibilityContract, record, wireContractSpec } from './contracts';
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

type DurableListReadWindow = Window & {
  _orchestrationDurableListEnvelope?: typeof orchestrationDurableListEnvelope;
  normalizeOrchestrationTaskListRead?: typeof normalizeOrchestrationTaskListRead;
};

export interface DurableListEnvelope {
  itemsField: string;
  pageField: string;
  pageFields: unknown[];
  limitField: string;
  hasMoreField: string;
  nextLimitField: string;
  maxLimit: number;
}

export function orchestrationDurableListEnvelope(
  source?: unknown,
): DurableListEnvelope {
  const defaults = record(
    compatibilityContract('durableRunContract')?.listEnvelope) ?? {};
  const contract = record(source);
  const published = record(contract?.listEnvelope) ?? {};
  return {
    itemsField: String(published.itemsField || defaults.itemsField),
    pageField: String(published.pageField || defaults.pageField),
    pageFields: Array.isArray(published.pageFields)
      ? published.pageFields : Array.isArray(defaults.pageFields)
        ? defaults.pageFields : [],
    limitField: String(published.limitField || defaults.limitField),
    hasMoreField: String(published.hasMoreField || defaults.hasMoreField),
    nextLimitField: String(
      published.nextLimitField || defaults.nextLimitField),
    maxLimit: Number.isSafeInteger(published.maxLimit)
      ? Number(published.maxLimit) : Number.MAX_SAFE_INTEGER,
  };
}

export function normalizeOrchestrationTaskListRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const wire = wireContractSpec('durable-run', options.durableRunContract);
  const envelope = orchestrationDurableListEnvelope(wire.contract);
  const runs = body[envelope.itemsField];
  const page = record(body[envelope.pageField]);
  const pagePresent = Object.prototype.hasOwnProperty.call(
    body, envelope.pageField);
  const limit = page?.[envelope.limitField];
  const hasMore = page?.[envelope.hasMoreField];
  const nextLimit = page?.[envelope.nextLimitField];
  const pageValid = !pagePresent || Boolean(page)
    && envelope.pageFields.every((field) => typeof field === 'string'
      && Object.prototype.hasOwnProperty.call(page, field))
    && Number.isSafeInteger(limit) && Number(limit) > 0
    && Number(limit) <= envelope.maxLimit
    && typeof hasMore === 'boolean'
    && (nextLimit === null
      || Number.isSafeInteger(nextLimit) && Number(nextLimit) > Number(limit)
        && Number(nextLimit) <= envelope.maxLimit);
  const accepted = read.recognized && body.ok === true && wire.supported
    && orchestrationRequiredResponseFieldsMatch(body, options)
    && pageValid && Array.isArray(runs)
    && runs.every((run) => orchestrationDurableRunMatches(
      run, 'listFields', wire.contract, options.runContract));
  return {
    ok: accepted,
    status: read.status,
    reason: !wire.supported ? 'unsupported-format'
      : orchestrationActionReason(read, accepted, 'list-rejected'),
    runs: accepted ? runs.map((run) =>
      projectOrchestrationDurableRunSnapshot(run, wire.contract)) : [],
    pageLimit: accepted && page ? limit : 0,
    hasMore: accepted && page ? hasMore : false,
    nextLimit: accepted && page ? nextLimit : null,
    expectedFormat: wire.expected,
    contractFormat: wire.actual,
    unsupportedFormat: !wire.supported,
    response: value,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

registerOrchestrationHttpReadProjectors({
  'task-list': normalizeOrchestrationTaskListRead,
});

Object.assign(orchestrationRegistry as unknown as DurableListReadWindow, {
  _orchestrationDurableListEnvelope: orchestrationDurableListEnvelope,
  normalizeOrchestrationTaskListRead,
});
