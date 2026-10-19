/** Presentation is not delivery/acceptance authority and never rewrites Turns. */
import { describe, expect, it, vi } from 'vitest';
import type { Translator } from '../../i18n';
import { createToolResultPresentation } from './tool-result-presentation';
import { TOOL_RESULT_SURFACES } from './tool-result-surfaces.generated';
import { renderSendAgentMessageHtml } from './send-agent-message-presentation';

// Test-only Node ports are loaded through Vitest; browser compilation has no Node ambient types.
const { readFileSync } = await vi.importActual<{
  readFileSync(path: URL, encoding: 'utf8'): string;
}>('node:fs');
const vm = await vi.importActual<{
  createContext(values: Record<string, unknown>): unknown;
  runInContext(source: string, context: unknown): (...args: unknown[]) => string;
}>('node:vm');
const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');
const messages = JSON.parse(readFileSync(new URL('../../i18n/locales/en.json', import.meta.url), 'utf8')) as Record<string, string>;
const translate: Translator = (key, ...args) => (messages[key] || key).replace(/\{(\w+)\}/g,
  (_, name: string) => String((args[0] as Record<string, unknown> | undefined)?.[name] ?? ''));
const header = { iconHtml: '<svg></svg>', queryHtml: 'OLD RECEIPT HEADER', rootPillHtml: '<span>root</span>',
  badgeHtml: 'OLD OK BADGE', rightControlsHtml: '<button data-test="inspector">Inspect</button>' };
const presentation = createToolResultPresentation({ translate,
  writeGateRefusal: { resolveRefusal: () => null, renderNoticeHtml: () => '' } });
const args = { agent_id: 'researcher', swarm_run_id: 'old-run', idempotency_key: 'stable-send-key',
  kind: 'reference', content: 'Please inspect <script>alert(1)</script> & keep\nall lines.' };
const receipt = { agent_id: args.agent_id, swarm_run_id: args.swarm_run_id, message_id: 'dedup-key',
  state: 'submitted', kind: 'reference', contentChars: Array.from(args.content).length, execution_started: false };
const round = { toolName: 'send_agent_message', roundNum: 8, status: 'done', toolArgs: args,
  toolContent: JSON.stringify(receipt) };
function render(value: unknown = round): string { return presentation.renderGenericResultHtml(value, {}, header); }
function inspect(html: string, check: (document: Document) => void): void {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`);
  try { check(dom.window.document); } finally { dom.window.close(); }
}

describe('authored send-message card', () => {
  it.each(['object', 'JSON'] as const)('shows complete escaped %s args, not bookkeeping JSON', format => {
    const value = { ...round, toolArgs: format === 'object' ? args : JSON.stringify(args), _deferToolResultBody: true };
    const before = JSON.stringify(value);
    inspect(render(value), document => {
      expect(document.querySelector('summary')?.textContent).toContain('Message to researcher');
      expect(document.querySelector('code')?.textContent).toBe(args.content);
      expect(document.querySelector('details')?.open).toBe(true);
      expect(document.querySelector('details')?.getAttribute('data-rn')).toBe('8');
      expect(document.querySelector('[data-test="inspector"]')).not.toBeNull();
      expect(document.querySelector('script, img')).toBeNull();
      for (const hidden of ['old-run', 'dedup-key', 'contentChars', 'execution_started', 'OLD OK BADGE', 'OLD RECEIPT HEADER']) {
        expect(document.body.textContent).not.toContain(hidden);
      }
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageSubmitted']);
    });
    expect(JSON.stringify(value)).toBe(before);
  });

  it.each(TOOL_RESULT_SURFACES.sendMessageReceipt.legacyStates)(
    'recognizes compact and persisted %s receipt states without claiming acceptance', state => {
      const outcome = TOOL_RESULT_SURFACES.sendMessageReceipt.outcomes[state];
      for (const source of [outcome, JSON.stringify({ ...receipt, state })]) {
        inspect(render({ ...round, toolContent: source }), document => {
          expect(document.querySelector('.ptool-agent-message')).not.toBeNull();
          expect(document.querySelector('code')?.textContent).toBe(args.content);
          const key = state === 'submitted' ? 'tool.agentMessageSubmitted'
            : state === 'input_confirmed' ? 'tool.agentMessageInputConfirmed' : 'tool.agentMessageUnconfirmed';
          expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages[key]);
          expect(document.querySelector('.ptool-line-status--ok')).toBeNull();

          if (state === 'unconfirmed') expect(document.body.textContent).toContain(messages['tool.agentMessageUnconfirmedAction']);
        });
      }
    });

  it('reads the declared historical result.content source, not frontend result metadata', () => {
    const { toolContent: _unused, ...rest } = round;
    inspect(render({ ...rest, result: { content: JSON.stringify(receipt) },
      results: [{ content: 'Metadata must not be sent content' }] }), document => {
      expect(document.querySelector('code')?.textContent).toBe(args.content);
      expect(document.querySelector('details')?.getAttribute('data-tool-result-authority')).toBe('roundResult');
    });
  });

  it('does not clip a valid 4096-code-point emoji message or escape it twice', () => {
    const content = '😃'.repeat(4096);
    inspect(render({ ...round, toolArgs: { ...args, content } }), document => {
      expect(document.querySelector('code')?.textContent).toBe(content);
      expect(document.querySelector('.ptool-result-trunc')).toBeNull();
    });
  });

  it('escapes the target and explicitly discloses missing content instead of recreating it', () => {
    inspect(render({ ...round, toolArgs: { agent_id: '<img src=x>', swarm_run_id: 'old-run' },
      toolContent: TOOL_RESULT_SURFACES.sendMessageReceipt.outcomes.submitted }), document => {
      expect(document.querySelector('summary')?.textContent).toContain('Message to <img src=x>');
      expect(document.querySelector('img')).toBeNull();
      expect(document.body.textContent).toContain(messages['tool.agentMessageUnavailable']);
    });
  });

  it.each(['searching', 'running', 'pending', 'aborted', 'interrupted', 'rejected'])(
    'leaves %s state to its existing card owner', status => {
      expect(renderSendAgentMessageHtml({ ...round, status }, header, translate, '')).toBe('');
    });

  it.each([
    { ...receipt, state: 'accepted' }, { ...receipt, extra: 'unknown metadata' },
    { ...receipt, execution_started: true }, { ...receipt, agent_id: 'different-agent' },
    { ...receipt, swarm_run_id: 'replacement-run' }, { ...receipt, contentChars: '12' },
  ])('never promotes an unsupported/mismatched receipt to submitted (%j)', unknown => {
    inspect(render({ ...round, toolContent: JSON.stringify(unknown) }), document => {
      expect(document.querySelector('.ptool-agent-message')).toBeNull();
      expect(document.querySelector('code')?.textContent).toContain(JSON.stringify(unknown, null, 2));
    });
  });

  it('leaves get_agent_messages receipts and idempotency evidence unchanged', () => {
    inspect(render({ ...round, toolName: 'get_agent_messages' }), document => {
      expect(document.querySelector('.ptool-agent-message')).toBeNull();
      expect(document.querySelector('code')?.textContent).toContain('dedup-key');
      expect(document.querySelector('code')?.textContent).toContain('old-run');
    });
  });

  it('keeps historical lazy references under the existing loader rather than inferring a receipt', () => {
    inspect(render({ ...round, _snapshotToolResult: { preview: JSON.stringify(receipt), chars: 2048 } }), document => {
      expect(document.querySelector('.ptool-agent-message')).toBeNull();
      expect(document.querySelector('details[data-lazy-tool-result]')).not.toBeNull();
    });
  });
});

describe('actionable failures without invented submission', () => {
  const error = { status: 'error', code: 'swarm_message_storage_failed', retryable: true,
    message: 'Storage acknowledgement uncertain <script>bad()</script>',
    next_action: 'Query the exact run receipt; reuse the SAME idempotency_key and unchanged content. Do not use a replacement run.',
    error: { code: 'swarm_message_storage_failed', retryable: true,
      message: 'Storage acknowledgement uncertain <script>bad()</script>',
      nextAction: 'Query the exact run receipt; reuse the SAME idempotency_key and unchanged content. Do not use a replacement run.' } };
  it.each(['done', 'error', 'failed'])('shows code/reason/reconciliation for %s typed errors', status => {
    inspect(render({ ...round, status, toolContent: JSON.stringify(error) }), document => {
      expect(document.querySelector('code')?.textContent).toBe(args.content);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(error.code);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(error.message);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(error.next_action);
      expect(document.body.textContent).not.toContain('"retryable"');
      expect(document.body.textContent).not.toContain(messages['tool.agentMessageSubmitted']);
      expect(document.querySelector('script')).toBeNull();
    });
  });
  it('keeps plain-text errors and explicitly missing failure evidence actionable', () => {
    for (const toolContent of ['Error: denied\nReuse <unchanged> arguments', '']) {
      inspect(render({ ...round, status: 'error', toolContent }), document => {
        expect(document.querySelector('.ptool-agent-message-error')?.textContent).toBe(toolContent || messages['tool.agentMessageErrorUnavailable']);
        expect(document.querySelector('unchanged')).toBeNull();
      });
    }
  });
  it('routes the retained error branch through the same typed card port', () => {
    const retained = readFileSync(new URL('../../runtime/sections/ui/tool_rounds.js', import.meta.url), 'utf8');
    const start = retained.indexOf('function _renderErrorRow(');
    const end = retained.indexOf('/* Command timing', start);
    const context = vm.createContext({ renderToolBrowserExecutionHtml: () => '',
      _rowRightControls: () => header.rightControlsHtml, _cmdBodyExpanded: new Set(), _cmdOutputExpanded: new Set(),
      renderToolArtifactHeaderHtml: () => '', _EDIT_FAILURE_TOOL_NAMES: new Set(),
      renderGenericToolResultHtml: presentation.renderGenericResultHtml });
    vm.runInContext(retained.slice(start, end), context);
    const html = vm.runInContext('_renderErrorRow', context)({ ...round, status: 'error', toolContent: JSON.stringify(error) },
      { svg: '', td: {}, meta: {}, rootPill: '', q: 'Message to researcher' });
    inspect(html, document => {
      expect(document.querySelector('.ptool-agent-message.ptool-error')).not.toBeNull();
      expect(document.querySelector('code')?.textContent).toBe(args.content);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain('SAME idempotency_key');
    });
  });
});


const stableArgs = { retained_agent_id: 'persistent-colleague', scope: 'project', intent: 'message',
  content: args.content, kind: 'reference', idempotency_key: 'stable-key' };
const assignment = { agent_id: 'ordinary-child', swarm_run_id: 'new-owned-run', conversation_id: 'child-dialogue' };
const launch = { status: 'async_launched', swarm_key: 'ordinary-swarm',
  agents: [{ id: assignment.agent_id, task_id: 'execution-task', conversation_id: assignment.conversation_id,
    swarm_run_id: assignment.swarm_run_id }] };
const stableReceipt = { retained_agent_id: stableArgs.retained_agent_id, scope: 'project', message_id: 'stable-message',
  state: 'turn_started', kind: 'reference', contentChars: Array.from(args.content).length,
  execution_started: true, assignment: { ...assignment, launch } };
const stableRound = { ...round, toolArgs: stableArgs, toolContent: JSON.stringify(stableReceipt) };

describe('stable target authored cards', () => {
  it.each(['project', 'conversation'])('shows %s new turn accepted, never input confirmation', scope => {
    const value = { ...stableRound, toolArgs: { ...stableArgs, scope },
      toolContent: JSON.stringify({ ...stableReceipt, scope }) };
    const before = JSON.stringify(value);
    inspect(render(value), document => {
      expect(document.querySelector('summary')?.textContent).toContain('Message to persistent-colleague');
      expect(document.querySelector('code')?.textContent).toBe(args.content);
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageTurnStarted']);
      expect(document.querySelector('.ptool-line-status--ok')).toBeNull();
      expect(document.querySelector('script')).toBeNull();
      for (const hidden of ['ordinary-child', 'new-owned-run', 'child-dialogue', 'execution-task', 'contentChars']) {
        expect(document.body.textContent).not.toContain(hidden);
      }
    });
    expect(JSON.stringify(value)).toBe(before);
  });

  it.each(TOOL_RESULT_SURFACES.sendMessageReceipt.legacyStates)('shows busy %s with pinned assignment retained', state => {
    const value = { ...stableRound, toolContent: JSON.stringify({ ...stableReceipt, state,
      execution_started: false, assignment }) };
    inspect(render(value), document => {
      expect(document.querySelector('.ptool-agent-message')).not.toBeNull();
      const key = state === 'submitted' ? 'tool.agentMessageSubmitted'
        : state === 'input_confirmed' ? 'tool.agentMessageInputConfirmed' : 'tool.agentMessageUnconfirmed';
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages[key]);
      expect(document.querySelector('code')?.textContent).toBe(args.content);
    });
    expect(JSON.parse(value.toolContent).assignment).toEqual(assignment);
  });

  it('uses resolved scope when the caller omitted it and escapes a stable target', () => {
    const target = '<img src=x onerror=bad()>';
    inspect(render({ ...stableRound, toolArgs: { retained_agent_id: target, content: args.content },
      toolContent: JSON.stringify({ ...stableReceipt, retained_agent_id: target }) }), document => {
      expect(document.querySelector('summary')?.textContent).toContain(`Message to ${target}`);
      expect(document.querySelector('img, script')).toBeNull();
    });
  });

  it.each([
    { ...stableReceipt, retained_agent_id: 'other-colleague' }, { ...stableReceipt, scope: 'conversation' },
    { ...stableReceipt, scope: 'global' }, { ...stableReceipt, execution_started: false },
    { ...stableReceipt, state: 'submitted' }, { ...stableReceipt, extra: 'unknown' },
    { ...stableReceipt, assignment: {} }, { ...stableReceipt, assignment: { ...assignment, conversation_id: '' } },
    { ...stableReceipt, assignment: { ...assignment, launch: { status: 'error' } } },
    { ...stableReceipt, assignment: { ...assignment, invented: true } },
    { ...stableReceipt, contentChars: true }, { ...stableReceipt, kind: 'other' },
  ])('does not certify invalid/mismatched stable receipts (%j)', value => {
    expect(renderSendAgentMessageHtml({ ...stableRound, toolContent: JSON.stringify(value) }, header, translate, '')).toBe('');
  });

  it('does not use legacy receipts or bare outcome text to certify stable sends', () => {
    for (const toolContent of [JSON.stringify(receipt), ...Object.values(TOOL_RESULT_SURFACES.sendMessageReceipt.outcomes)]) {
      expect(renderSendAgentMessageHtml({ ...stableRound, toolContent }, header, translate, '')).toBe('');
    }
    expect(renderSendAgentMessageHtml({ ...round, toolContent: JSON.stringify(stableReceipt) }, header, translate, '')).toBe('');
  });
});

describe('explicit rejected versus unknown stable submissions', () => {
  const diagnostic = { status: 'error', code: 'retained_agent_not_found',
    message: 'Raw reason <script>bad()</script>', next_action: 'Inspect SAME stable key; do not start replacement work.' };
  it.each([
    { submission_state: 'rejected' }, { error: { submission_state: 'rejected' } },
  ])('renders definite rejection only from explicit evidence (%j)', evidence => {
    inspect(render({ ...stableRound, status: 'error', toolContent: JSON.stringify({ ...diagnostic, ...evidence }) }), document => {
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageRejected']);
      expect(document.querySelector('code')?.textContent).toBe(args.content);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(diagnostic.code);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(diagnostic.message);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(diagnostic.next_action);
      expect(document.querySelector('script')).toBeNull();
    });
  });

  it.each([
    {}, { submission_state: 'unknown' }, { submission_state: 'unsupported' },
    { submission_state: 'unknown', error: { submission_state: 'rejected' } },

    { submission_state: null, error: { submission_state: 'rejected' } },
  ])('keeps rejection-like codes uncertain without authoritative rejection (%j)', evidence => {
    inspect(render({ ...stableRound, toolContent: JSON.stringify({ ...diagnostic, ...evidence }) }), document => {
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageFailed']);
      expect(document.body.textContent).not.toContain(messages['tool.agentMessageRejected']);
    });
  });

  it.each(['stable', 'legacy'])('shows saved-but-unconfirmed %s receipt inside an error with diagnostics', format => {
    const embedded = format === 'stable' ? { ...stableReceipt, state: 'unconfirmed', execution_started: false, assignment }
      : { ...receipt, state: 'unconfirmed' };
    const value = format === 'stable' ? stableRound : round;
    inspect(render({ ...value, status: 'error', toolContent: JSON.stringify({ ...diagnostic, submission_state: 'unknown', receipt: embedded }) }), document => {
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageSavedUnconfirmed']);
      expect(document.body.textContent).toContain(messages['tool.agentMessageUnconfirmedAction']);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(diagnostic.message);
      expect(document.querySelector('.ptool-agent-message-error')?.textContent).toContain(diagnostic.next_action);
    });
  });

  it('does not let a foreign embedded receipt prove that this message was saved', () => {
    const embedded = { ...stableReceipt, retained_agent_id: 'foreign', state: 'unconfirmed', execution_started: false, assignment };
    inspect(render({ ...stableRound, status: 'error', toolContent: JSON.stringify({ ...diagnostic, receipt: embedded }) }), document => {
      expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageFailed']);
    });
  });
});


it('keeps generic summaries and distinct raw diagnostics inspectable and escaped', () => {
  const error = { status: 'error', submission_state: 'unknown', code: 'raw-code', summary: 'Generic uncertain summary',
    message: 'Original producer diagnostic <script>raw()</script>', error: { message: 'Nested raw cause <img src=x>' },
    next_action: 'Inspect the original stable key.' };
  inspect(render({ ...stableRound, status: 'error', toolContent: JSON.stringify(error) }), document => {
    const diagnostic = document.querySelector('.ptool-agent-message-error')?.textContent;
    for (const line of [error.code, error.summary, error.message, error.error.message, error.next_action]) {
      expect(diagnostic).toContain(line);
    }
    expect(document.querySelector('script, img')).toBeNull();
    expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageFailed']);
  });
});

it('recognizes a stable receipt with optional launch absent without inventing provider proof', () => {
  inspect(render({ ...stableRound, toolContent: JSON.stringify({ ...stableReceipt, assignment }) }), document => {
    expect(document.querySelector('.ptool-line-status')?.textContent).toBe(messages['tool.agentMessageTurnStarted']);
    expect(document.querySelector('code')?.textContent).toBe(args.content);
  });
});
