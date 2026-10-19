/** Real client event-to-DOM fixture. Only snapshot/stream I/O is synthetic.
 * Exposes measurement and reader assertions; never changes a production owner.
 */
import { createConversationTurnRuntime } from '../../frontend/src/core/turn-runtime';
import { createConversationSurfaceController } from '../../frontend/src/conversation/application/conversation-surface-controller';
import { createClassicConversationRenderers } from '../../frontend/src/conversation/ui/classic-conversation-renderers';
import { createMarkdownRenderer } from '../../frontend/src/markdown';
import { installMarkdownPolicy } from '../../frontend/src/markdown-policy';
import { swarmPresentationOverlay } from '../../frontend/src/conversation/application/swarm-presentation-overlay';

export async function createTurnPipelineFixture(count: number, receiptCount = 0) {
  const globals = globalThis as any;
  installMarkdownPolicy(globals.marked);
  const markdown = createMarkdownRenderer({ maximumCacheEntries: 300,
    getEnvironment: () => ({document, parser: globals.marked, sanitizer: globals.DOMPurify,
      highlighter: null, mathRenderer: null, basePath: '', projectActive: false}), ensureMath() {} });
  const container = document.createElement('main');
  container.id = 'pipeline-surface'; container.className = 'chat-inner';
  document.body.append(container);
  const conversation = {id: 'pipeline'};
  const turnId = `turn-${count - 1}`, attemptId = 'live-attempt';
  const turns = Array.from({length: count}, (_, index) => ({
    turnId: `turn-${index}`, presentationId: `turn-${index}`, conversationId: conversation.id,
    laneId: 'main', ordinal: index + 1, actor: 'assistant', kind: 'reply', runId: '',
    status: index === count - 1 ? 'running' : 'completed',
    currentAttemptId: index === count - 1 ? attemptId : '', projectionRevision: 1,
    projection: {content: `Evidence ${index}`, segments: [{type: 'text', blockId: 'answer',
      text: `Evidence ${index}: **stable** 中文 😀\n\nRetained source and verification.`,
      terminal: index !== count - 1, deliverable: index !== count - 1}]},
    settlement: {}, createdAt: 1, updatedAt: 1,
  }));
  if (receiptCount) {
    const receipts = Array.from({ length: receiptCount }, (_, index) => ({
      swarmRunId: 'pressure-run', agentId: 'agent', messageId: `message-${index}`,
      state: 'input_confirmed', content: `Steering ${index}`, inputConfirmedAt: index + 1,
    }));
    Object.assign(turns[count - 2].projection, { toolRounds: [
      { toolName: 'spawn_agents', _swarm: true, _swarmRunId: 'pressure-run',
        status: 'done', _swarmAgents: [{ id: 'agent', status: 'done', messageReceipts: receipts }] },
      ...Array.from({ length: 148 }, (_, index) => ({ toolName: 'run_command',
        status: 'done', toolContent: `Synthetic output ${index}: ${'x'.repeat(8192)}` })),
      ...receipts.map(receipt => ({ toolName: 'send_agent_message', status: 'done',
        toolArgs: { content: receipt.content }, toolContent: JSON.stringify({
          swarm_run_id: receipt.swarmRunId, agent_id: receipt.agentId, message_id: receipt.messageId,
        }) })),
    ] });
  }
  let source: any, runtime: ReturnType<typeof createConversationTurnRuntime>;
  let sequence = 0, pending: any = null, commits = 0, renderMs = 0;
  const errors: string[] = [], receipts: unknown[] = [];
  const frames = new Set<number>();
  const frame = () => new Promise<void>(resolve => {
    const id = requestAnimationFrame(() => { frames.delete(id); resolve(); }); frames.add(id);
  });
  const observer = new MutationObserver(() => {
    if (!pending) return;
    const observed = performance.now();
    const actual = container.querySelector(`[data-turn-id="${turnId}"]`);
    if (!actual?.textContent?.includes(pending.expected)) return;
    const receipt = pending; pending = null; clearTimeout(receipt.timeout);
    receipt.resolve({domMs: observed - receipt.started, dispatchMs: receipt.dispatchMs,
      renderMs: renderMs - receipt.renderBefore, commits: commits - receipt.commitsBefore});
  });
  observer.observe(container, {subtree: true, childList: true, characterData: true});
  const controller = createConversationSurfaceController({
    isActive: id => id === conversation.id, getContainer: () => container,
    nativeRenderers: createClassicConversationRenderers({
      renderSafeMarkdownHtml: (text, options) => markdown.render(text, options),
    }),
    schedule(render) {
      const id = requestAnimationFrame(() => {
        frames.delete(id); const start = performance.now(); render(); renderMs += performance.now() - start;
      });
      frames.add(id); return () => { cancelAnimationFrame(id); frames.delete(id); };
    },
    afterConversationCommit(conv, state, _force, _model, mountedIds) {
      commits++; runtime.notifyRenderedState(conv.id, state, mountedIds);
    },
  });
  runtime = createConversationTurnRuntime({
    api: {snapshot: async () => ({ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
      scope: {kind: 'conversation', id: conversation.id}, conversationId: conversation.id,
      conversationRevision: 1, syncSeq: 0, cursor: 'cursor-0', serverBootId: 'fixture',
      heartbeatIntervalMs: 15000, settings: {}, turns, queueItems: [], attempts: [{
        attemptId, conversationId: conversation.id, turnId, commandId: 'command', taskId: 'task',
        operation: 'submit', status: 'running', baseProjectionRevision: 0, resumeAnchor: {},
        createdAt: 1, startedAt: 1, lastSeq: 0,
      }]}), eventsUrl: () => 'fixture-stream', recordPerception: async (_c: unknown, _t: unknown, body: unknown) => {receipts.push(body);} } as any,
    findConversation: () => conversation, isActive: () => true,
    renderState: (conv, state, context) => controller.render(conv,
      receiptCount ? swarmPresentationOverlay.compose(conv.id, state, state, () => null) : state, context),
    isTurnMounted: (_conv, id) => !!container.querySelector(`[data-turn-id="${id}"]`),
    getTurnMountToken: (_conv, id) => container.querySelector(`[data-turn-id="${id}"]`) ? id : null,
    eventSourceFactory: () => source = {addEventListener() {}, removeEventListener() {}, close() {}} as any,
    onProtocolError: error => errors.push(String(error)), onResyncError: error => errors.push(String(error)),
  });
  const initialStarted = performance.now();
  await runtime.hydrateConversation(conversation);
  for (let i = 0; i < 60 && !container.querySelector(`[data-turn-id="${turnId}"]`); i++) await frame();
  if (!source || !container.querySelector(`[data-turn-id="${turnId}"]`)) throw Error('Initial production graph did not mount');
  const initialMs = performance.now() - initialStarted;
  const retained = container.querySelector(`[data-turn-id="turn-${count - 2}"]`)!;
  if (!retained) throw Error('Reader turn missing');
  const input = document.createElement('input'); input.value = 'My steering draft'; retained.append(input);
  const samples: any[] = [];
  const send = (number: number, terminal = false) => {
    sequence++;
    const expected = `Live evidence ${number}`;
    const projection = {content: expected, segments: [{type: 'text', blockId: 'answer',
      text: `## ${expected}\n\n**Checked** 中文 😀\n\n| Source | Result |\n| --- | --- |\n| retained | ${number} |`,
      terminal, deliverable: terminal}]};
    source.onmessage({lastEventId: `cursor-${sequence}`, data: JSON.stringify({
      contract: 'tofu.conversation-sync.event/v1', conversationId: conversation.id,
      type: 'attempt.event', syncSeq: sequence, occurredAt: Date.now(), payload: {event: {
        conversationId: conversation.id, turnId, attemptId, seq: sequence,
        projectionRevision: sequence + 1, type: terminal ? 'terminal_settlement' : 'projection_updated',
        payload: {projection, ...(terminal ? {status: 'completed', settlement: {}} : {})},
      }},
    })});
    return expected;
  };
  async function step(number: number, burst = 1, terminal = false) {
    await frame();
    return new Promise<any>((resolve, reject) => {
      const receipt: any = {started: performance.now(), expected: `Live evidence ${number + burst - 1}`,
        resolve, reject, renderBefore: renderMs, commitsBefore: commits};
      receipt.timeout = setTimeout(() => { pending = null; reject(Error('Event did not reach DOM: '+JSON.stringify(errors))); }, 5000);
      pending = receipt;
      for (let offset = 0; offset < burst; offset++) send(number + offset, terminal && offset === burst - 1);
      receipt.dispatchMs = performance.now() - receipt.started;
    });
  }
  return {
    async measure(iterations = 64) {
      for (let index = 0; index < iterations; index++) {
        const sample = await step(index);
        if (index >= 4) samples.push(sample);
      }
      input.focus(); input.setSelectionRange(3, 7);
      const burst = await step(1000, 20);
      if (document.activeElement !== input || input.value !== 'My steering draft'
          || input.selectionStart !== 3 || input.selectionEnd !== 7
          || container.querySelector(`[data-turn-id="turn-${count - 2}"]`) !== retained) throw Error('Reader state lost');
      const terminal = await step(2000, 1, true);
      if (runtime.readRuntimeState(conversation.id)?.turnsById[turnId]?.status !== 'completed') throw Error('Terminal authority missing');
      if (errors.length) throw Error(errors.join('\n'));
      return {count, initialMs, samples, burst, terminal, readerPreserved: true,
        mountedTurns: container.querySelectorAll('[data-turn-id]').length,
        text: container.textContent, html: container.innerHTML, receipts: receipts.length};
    },
    close() {
      observer.disconnect(); controller.dispose(); runtime.disposeConversation(conversation.id); markdown.dispose();
      for (const id of frames) cancelAnimationFrame(id); frames.clear();
      if (pending) {clearTimeout(pending.timeout); pending.reject(Error('Fixture disposed'));pending = null;}
      container.remove();
    },
  };
}
