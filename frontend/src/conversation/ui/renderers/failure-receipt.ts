/**
 * Structured, selectable failure receipt DOM projection. Reads typed
 * attribution and identity; no retry or transport authority.
 * Entry points: buildFailureReceipt.
 * Dependencies: errors, conversation-view-model, ports, localized-copy.
 */
import { errorFailureAttribution } from '../../../api/errors';
import type { ErrorEnvelope } from '../../../api/errors';
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { textFor } from './localized-copy';

async function copyDiagnosticsText(document: Document, text: string): Promise<boolean> {
  try {
    const clipboard = document.defaultView?.navigator.clipboard;
    if (clipboard) {
      await clipboard.writeText(text);
      return true;
    }
  } catch {
    // A denied native clipboard write falls through to the local DOM path.
  }
  const previousFocus = document.activeElement as HTMLElement | null;
  let area: HTMLTextAreaElement | null = null;
  try {
    area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', 'true');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    area?.remove();
    if (previousFocus?.isConnected && typeof previousFocus.focus === 'function') {
      try { previousFocus.focus({ preventScroll: true }); } catch { /* The copy verdict is final. */ }
    }
  }
}

/* One terminal failure = one structured receipt. The receipt line names the
 * exact execution identity (turn / attempt / round / tool) and the copy
 * button hands support a compact diagnostics bundle — error facts plus the
 * durable ids — so a failure report never depends on a screenshot or a manual
 * text selection inside the envelope <pre>. */
export function buildFailureReceipt(
  document: Document,
  terminalError: ErrorEnvelope,
  turn: ConversationTurnViewModel | undefined,
  ports: ClassicConversationRendererPorts,
): HTMLElement {
  const receipt = document.createElement('div');
  receipt.className = 'activity-event__failure-receipt';
  const attribution = errorFailureAttribution(terminalError);
  const facts: Array<[string, string]> = [];
  if (turn?.turnId) {
    facts.push([textFor(ports, 'activity.receiptTurn', 'Turn'), turn.turnId]);
  }
  if (turn?.attemptId) {
    facts.push([
      textFor(ports, 'activity.receiptAttempt', 'Attempt'), turn.attemptId,
    ]);
  }
  if (turn?.taskId) {
    facts.push([textFor(ports, 'activity.receiptTask', 'Task'), turn.taskId]);
  }
  if (attribution?.roundNum != null) {
    facts.push([
      textFor(ports, 'activity.receiptRound', 'Round'),
      String(attribution.roundNum),
    ]);
  }
  if (attribution?.toolName) {
    facts.push([
      textFor(ports, 'activity.receiptTool', 'Tool'), attribution.toolName,
    ]);
  }
  if (attribution?.phase) {
    facts.push([
      textFor(ports, 'activity.receiptPhase', 'Phase'), attribution.phase,
    ]);
  }
  if (facts.length) {
    const list = document.createElement('dl');
    list.className = 'activity-event__receipt-facts';
    for (const [label, value] of facts) {
      const term = document.createElement('dt');
      term.textContent = label;
      const description = document.createElement('dd');
      description.textContent = value;
      if (value.length > 24) description.title = value;
      list.append(term, description);
    }
    receipt.appendChild(list);
  }
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'activity-event__copy-diagnostics';
  copy.textContent = textFor(
    ports, 'activity.copyDiagnostics', 'Copy diagnostics');
  copy.addEventListener('click', () => {
    const diagnostics: Record<string, unknown> = {
      turnId: turn?.turnId ?? '',
      attemptId: turn?.attemptId ?? '',
      taskId: turn?.taskId ?? '',
      status: turn?.status ?? '',
      error: Object.fromEntries(
        ['kind', 'message', 'detail', 'raw', 'model', 'source', 'context',
          'severity', 'retryable', 'statusCode', 'failureAttribution'].flatMap((key) => {
          const value = terminalError[key];
          if (value == null || value === '') return [];
          if (key === 'raw' && value === terminalError.detail) return [];
          return [[key, value]];
        })),
    };
    const settlement = turn?.source?.settlement;
    if (settlement && typeof settlement === 'object') {
      diagnostics.settlement = Object.fromEntries(
        (['outcome', 'cause', 'evidence', 'providerFinishReason', 'retryChain'] as const)
          .filter((key) => settlement[key] != null)
          .map((key) => [key, settlement[key]]));
    }
    let serialized = '';
    try {
      serialized = JSON.stringify(diagnostics, null, 2);
    } catch {
      serialized = terminalError.message;
    }
    void copyDiagnosticsText(document, serialized).then((copied) => {
      copy.textContent = copied
        ? textFor(ports, 'activity.diagnosticsCopied', 'Copied')
        : textFor(ports, 'activity.copyDiagnostics', 'Copy diagnostics');
    });
  });
  receipt.appendChild(copy);
  return receipt;
}
