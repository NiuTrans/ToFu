import { orchestrationRegistry } from './registry';
import { record } from './contracts';

type DurableStartWindow = Window & {
  projectOrchestrationDurableStartOutcome?:
    typeof projectOrchestrationDurableStartOutcome;
};

export function projectOrchestrationDurableStartOutcome(result: unknown) {
  const value = record(result) ?? {};
  const transportAccepted = value.ok === true;
  const rawTarget = transportAccepted ? value.runId : value.failedRunId;
  const targetRunId = typeof rawTarget === 'string' ? rawTarget : '';
  const accepted = transportAccepted && Boolean(targetRunId);
  return Object.freeze({
    accepted,
    targetRunId,
    recoverableFailure: !transportAccepted && Boolean(targetRunId),
    error: typeof value.error === 'string' ? value.error : '',
  });
}

(orchestrationRegistry as unknown as DurableStartWindow).projectOrchestrationDurableStartOutcome =
  projectOrchestrationDurableStartOutcome;
