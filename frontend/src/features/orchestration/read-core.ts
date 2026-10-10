import { orchestrationRegistry } from './registry';
import { record, type ContractRecord, type ContractSource } from './contracts';
import { orchestrationRequiredResponseFieldsMatch } from './result';

export { orchestrationRequiredResponseFieldsMatch } from './result';

export type HttpReadBody = ContractRecord | unknown[];

export interface OrchestrationHttpRead {
  body: HttpReadBody;
  normalized: boolean;
  status: number;
  transportOk: boolean;
  envelope: boolean;
  recognized: boolean;
}

export interface OrchestrationReadOptions extends Record<string, unknown> {
  directOk?: (value: unknown) => boolean;
  requestArgs?: readonly unknown[];
  expectedTaskId?: unknown;
  liveReplay?: boolean;
  definitionEntryContract?: ContractSource;
  definitionListContract?: ContractSource;
  definitionWriteContract?: ContractSource;
  inspectionContract?: ContractSource;
  runtimeStartContract?: ContractSource;
  mutationContract?: ContractSource;
  durableRunContract?: ContractSource;
  replayContract?: ContractSource;
  runContract?: ContractSource;
  responseRequiredFields?: readonly string[];
}

export type OrchestrationHttpReadProjector = (
  value: unknown,
  options?: OrchestrationReadOptions,
) => unknown;

type ReadCoreWindow = Window & {
  _ORCHESTRATION_HTTP_READ_PROJECTORS?: Record<
    string, OrchestrationHttpReadProjector
  >;
  registerOrchestrationHttpReadProjectors?:
    typeof registerOrchestrationHttpReadProjectors;
  _orchestrationNormalizedHttpRead?: typeof orchestrationNormalizedHttpRead;
  _orchestrationHttpRead?: typeof orchestrationHttpRead;
  _orchestrationActionRead?: typeof orchestrationActionRead;
  _orchestrationHttpFailureReason?: typeof orchestrationHttpFailureReason;
  _orchestrationActionReason?: typeof orchestrationActionReason;
};

export const ORCHESTRATION_HTTP_READ_PROJECTORS: Record<
  string, OrchestrationHttpReadProjector
> = Object.create(null) as Record<string, OrchestrationHttpReadProjector>;

export function registerOrchestrationHttpReadProjectors(
  projectors?: Record<string, unknown> | null,
): void {
  Object.keys(projectors ?? {}).forEach((name) => {
    const projector = projectors?.[name];
    if (!name || typeof projector !== 'function') {
      const invalid = new Error(
        `Invalid orchestration HTTP-read projector: ${String(name || '')}`);
      invalid.name = 'OrchestrationHttpReadProjectorError';
      throw invalid;
    }
    const typed = projector as OrchestrationHttpReadProjector;
    const existing = ORCHESTRATION_HTTP_READ_PROJECTORS[name];
    if (existing && existing !== typed) {
      const duplicate = new Error(
        `Duplicate orchestration HTTP-read projector: ${name}`);
      duplicate.name = 'OrchestrationHttpReadProjectorError';
      throw duplicate;
    }
    ORCHESTRATION_HTTP_READ_PROJECTORS[name] = typed;
  });
}

export function orchestrationNormalizedHttpRead(value: unknown): boolean {
  const candidate = record(value);
  return Boolean(candidate)
    && Object.prototype.hasOwnProperty.call(candidate, 'status')
    && Object.prototype.hasOwnProperty.call(candidate, 'data');
}

export function orchestrationHttpRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
): OrchestrationHttpRead {
  const normalized = orchestrationNormalizedHttpRead(value);
  const source = record(value) ?? {};
  const rawBody = normalized ? source.data : value;
  const status = normalized ? Number(source.status || 0) : 0;
  const transportOk = normalized
    ? source.ok === true
    : typeof options.directOk === 'function'
      ? options.directOk(value)
      : Boolean(value) && typeof value === 'object';
  const body = rawBody && typeof rawBody === 'object'
    ? rawBody as HttpReadBody : {};
  const fields = body as ContractRecord;
  const envelope = typeof fields.ok === 'boolean';
  return {
    body,
    normalized,
    status,
    transportOk,
    envelope,
    recognized: transportOk && envelope,
  };
}

export function orchestrationActionRead(value: unknown): OrchestrationHttpRead {
  return orchestrationHttpRead(value);
}

export function orchestrationHttpFailureReason(
  read: OrchestrationHttpRead,
): string {
  if (read.transportOk) return 'malformed-response';
  if (read.status >= 500) return 'server-failed';
  return read.status > 0 ? 'request-rejected' : 'transport-failed';
}

export function orchestrationActionReason(
  read: OrchestrationHttpRead,
  accepted: boolean,
  rejectedReason: string,
): string {
  if (accepted) return 'accepted';
  if (read.transportOk && read.envelope
      && (read.body as ContractRecord).ok === false) {
    return rejectedReason;
  }
  return orchestrationHttpFailureReason(read);
}

Object.assign(orchestrationRegistry as unknown as ReadCoreWindow, {
  _ORCHESTRATION_HTTP_READ_PROJECTORS: ORCHESTRATION_HTTP_READ_PROJECTORS,
  registerOrchestrationHttpReadProjectors,
  _orchestrationNormalizedHttpRead: orchestrationNormalizedHttpRead,
  _orchestrationHttpRead: orchestrationHttpRead,
  _orchestrationActionRead: orchestrationActionRead,
  _orchestrationHttpFailureReason: orchestrationHttpFailureReason,
  _orchestrationActionReason: orchestrationActionReason,
});
