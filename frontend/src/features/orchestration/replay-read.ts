import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  record,
  wireContractSpec,
  type ContractRecord,
} from './contracts';
import {
  orchestrationDurableRunMatches,
  orchestrationRunStatuses,
  projectOrchestrationDurableRunSnapshot,
} from './durable-run-snapshot';
import {
  orchestrationActionRead,
  orchestrationActionReason,
  orchestrationRequiredResponseFieldsMatch,
  registerOrchestrationHttpReadProjectors,
  type OrchestrationReadOptions,
} from './read-core';
import { orchestrationResultError } from './result';

type ReplayReadWindow = Window & {
  _orchestrationReplayEventMatches?: typeof orchestrationReplayEventMatches;
  _orchestrationLiveReplayMatches?: typeof orchestrationLiveReplayMatches;
  _normalizeOrchestrationReplayRead?: typeof normalizeOrchestrationReplayRead;
  normalizeOrchestrationRunPollRead?: typeof normalizeOrchestrationRunPollRead;
  normalizeOrchestrationTaskEventsRead?:
    typeof normalizeOrchestrationTaskEventsRead;
};

export function orchestrationReplayEventMatches(
  value: unknown,
  source?: unknown,
): boolean {
  const event = record(value);
  if (!event) return false;
  const contract = record(source) ?? {};
  const defaults = compatibilityContract('replayContract') ?? {};
  const typeField = String(contract.eventTypeField || defaults.eventTypeField);
  const sequenceField = String(
    contract.eventSequenceField || defaults.eventSequenceField);
  const requiredFields = Array.isArray(contract.eventRequiredFields)
    ? contract.eventRequiredFields
    : Array.isArray(defaults.eventRequiredFields)
      ? defaults.eventRequiredFields : [];
  if (!requiredFields.every((field) => typeof field === 'string' && field
      && Object.prototype.hasOwnProperty.call(event, field))) return false;
  const sequence = event[sequenceField];
  return typeof event[typeField] === 'string' && Boolean(event[typeField])
    && typeof sequence === 'number' && Number.isSafeInteger(sequence)
    && sequence >= 0;
}

export function orchestrationLiveReplayMatches(
  body: ContractRecord,
  wire: { present: boolean },
  options: OrchestrationReadOptions,
): boolean {
  if (!options.liveReplay || !wire.present) return true;
  const requestArgs = Array.isArray(options.requestArgs)
    ? options.requestArgs : [];
  const expectedTaskId = String(
    options.expectedTaskId || requestArgs[0] || '');
  const taskId = typeof body.taskId === 'string' ? body.taskId : '';
  const clock = (value: unknown): boolean => value === null
    || Number.isSafeInteger(value) && Number(value) >= 0;
  return Boolean(taskId) && (!expectedTaskId || taskId === expectedTaskId)
    && Object.prototype.hasOwnProperty.call(body, 'createdAt')
    && Object.prototype.hasOwnProperty.call(body, 'updatedAt')
    && clock(body.createdAt) && clock(body.updatedAt);
}

export function normalizeOrchestrationReplayRead(
  value: unknown,
  rejectedReason: string,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const wire = inspectWireFormat(
    'task-replay', body, options.replayContract);
  const contract = wire.contract ?? {};
  const defaults = compatibilityContract('replayContract') ?? {};
  const eventsField = String(contract.eventsField || defaults.eventsField);
  const terminalField = String(
    contract.terminalField || defaults.terminalField);
  const caughtUpField = String(
    contract.caughtUpField || defaults.caughtUpField);
  const statusField = String(contract.statusField || defaults.statusField);
  const nextCursorField = String(
    contract.nextCursorField || defaults.nextCursorField);
  const defaultCursor = record(defaults.cursor) ?? {};
  const cursorContract = record(contract.cursor) ?? defaultCursor;
  const cursorField = String(cursorContract.field || defaultCursor.field);
  const cursorRequestedField = String(
    cursorContract.requestedField || defaultCursor.requestedField);
  const cursorNextField = String(
    cursorContract.nextField || defaultCursor.nextField);
  const cursorResetField = String(
    cursorContract.resetField || defaultCursor.resetField);
  const defaultSnapshot = record(defaults.terminalSnapshot) ?? {};
  const snapshotContract = record(contract.terminalSnapshot)
    ?? defaultSnapshot;
  const snapshotField = String(snapshotContract.field || defaultSnapshot.field);
  const snapshotWhen = record(snapshotContract.when);
  const events = body[eventsField];
  const eventsMatch = Array.isArray(events) && events.every(
    (event) => orchestrationReplayEventMatches(event, contract));
  const nextCursor = body[nextCursorField];
  const nextCursorMatches = typeof nextCursor === 'number'
    && Number.isSafeInteger(nextCursor) && nextCursor >= 0;
  const runStatus = body[statusField];
  const statuses = orchestrationRunStatuses(options.runContract);
  const statusMatches = typeof runStatus === 'string' && Boolean(runStatus)
    && (!Array.isArray(statuses) || statuses.includes(runStatus));
  const cursor = record(body[cursorField]);
  const cursorPresent = Boolean(cursor);
  const cursorMatches = cursorPresent
    && Number.isSafeInteger(cursor?.[cursorRequestedField])
    && Number(cursor?.[cursorRequestedField]) >= 0
    && Number.isSafeInteger(cursor?.[cursorNextField])
    && cursor?.[cursorNextField] === nextCursor
    && typeof cursor?.[cursorResetField] === 'boolean';
  const caughtUpPresent = Object.prototype.hasOwnProperty.call(
    body, caughtUpField);
  const caughtUpMatches = !caughtUpPresent
    || typeof body[caughtUpField] === 'boolean';
  const pageFields = Array.isArray(contract.pageFields)
    ? contract.pageFields
    : Array.isArray(defaults.pageFields) ? defaults.pageFields : [];
  const pageFieldsMatch = !wire.present || pageFields.every(
    (field) => typeof field === 'string'
      && Object.prototype.hasOwnProperty.call(body, field));
  const pageMatches = pageFieldsMatch && nextCursorMatches && statusMatches
    && caughtUpMatches
    && orchestrationLiveReplayMatches(body, wire, options)
    && (cursorMatches || (!wire.present && !cursorPresent));
  const accepted = read.recognized && body.ok === true
    && wire.supported
    && (!read.normalized || !wire.present
      || orchestrationRequiredResponseFieldsMatch(body, options))
    && eventsMatch && pageMatches
    && typeof body[terminalField] === 'boolean';
  const page: ContractRecord = { ...body };
  page.ok = accepted;
  page.httpStatus = read.status;
  page.reason = !wire.supported ? 'unsupported-format'
    : read.recognized && body.ok === true && !eventsMatch
      ? 'malformed-events'
      : read.recognized && body.ok === true && !pageMatches
        ? 'malformed-page'
        : orchestrationActionReason(read, accepted, rejectedReason);
  if (!accepted && read.status === 404) page.reason = 'not-found';
  page.notFound = page.reason === 'not-found';
  page.events = accepted ? body[eventsField] : [];
  page.done = accepted ? body[terminalField] : false;
  page.caught_up = accepted ? body[caughtUpField] !== false : false;
  page.replayComplete = Boolean(page.done && page.caught_up);
  page.status = accepted ? runStatus : '';
  page.next_cursor = accepted ? nextCursor : 0;
  page.cursor = accepted && cursorPresent ? {
    requested: cursor?.[cursorRequestedField],
    next: cursor?.[cursorNextField],
    reset: cursor?.[cursorResetField],
  } : null;
  const snapshotAllowed = !snapshotWhen
    || body[String(snapshotWhen.field || terminalField)]
      === snapshotWhen.equals;
  const snapshot = accepted && snapshotAllowed
    ? record(body[snapshotField]) : null;
  const durableWire = wireContractSpec(
    'durable-run', options.durableRunContract);
  const durableSnapshot = !snapshot || (
    durableWire.supported && orchestrationDurableRunMatches(
      snapshot,
      'readFields',
      durableWire.contract,
      options.runContract,
      Array.isArray(options.requestArgs) ? options.requestArgs[0] : '',
    )
  );
  if (!durableSnapshot) {
    page.ok = false;
    page.reason = !durableWire.supported
      ? 'unsupported-format' : 'malformed-run-snapshot';
    page.events = [];
    page.done = false;
    page.caught_up = false;
    page.replayComplete = false;
  }
  page.run = page.ok && durableSnapshot && snapshot
    ? projectOrchestrationDurableRunSnapshot(snapshot, durableWire.contract)
    : null;
  page.runId = record(page.run)?.id || '';
  page.runContractFormat = durableWire.actual;
  page.unsupportedRunFormat = !durableWire.supported;
  page.canonical = wire.present && wire.supported;
  page.unsupportedFormat = !wire.supported;
  page.expectedFormat = wire.expected;
  page.wireFormat = wire.actual;
  page.response = value;
  page.errorMessage = page.ok ? '' : orchestrationResultError(value, '');
  return page;
}

export function normalizeOrchestrationRunPollRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  return normalizeOrchestrationReplayRead(
    value, 'poll-rejected', { ...options, liveReplay: true });
}

export function normalizeOrchestrationTaskEventsRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  return normalizeOrchestrationReplayRead(value, 'events-rejected', options);
}

registerOrchestrationHttpReadProjectors({
  'run-poll': normalizeOrchestrationRunPollRead,
  'task-events': normalizeOrchestrationTaskEventsRead,
});

Object.assign(orchestrationRegistry as unknown as ReplayReadWindow, {
  _orchestrationReplayEventMatches: orchestrationReplayEventMatches,
  _orchestrationLiveReplayMatches: orchestrationLiveReplayMatches,
  _normalizeOrchestrationReplayRead: normalizeOrchestrationReplayRead,
  normalizeOrchestrationRunPollRead,
  normalizeOrchestrationTaskEventsRead,
});
