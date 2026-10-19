// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { backgroundAgentContextHost, createAgentSidebarDock, type AgentDockItem } from './agent-sidebar-dock';
import { createConversationSurface } from './conversation/ui/conversation-surface';
const translate = (_: string, fallback: string, vars?: Record<string, string | number>) => fallback.replace('{count}', String(vars?.count ?? ''));
const item = (key: string, status = 'running'): AgentDockItem => ({ key, name: `Agent ${key}`, status, revision: '1', render: vi.fn(async () => '<details data-rich-key="call-1"><summary>read_files</summary><pre>source</pre></details>') });
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };

const execution = (key: string, producerTask = 'producer', workerTask = '', scope = 'turn/attempt/run/agent'): AgentDockItem => ({
  ...item(key), identity: { scope, producerTask, workerTask },
});
function setup(items: AgentDockItem[]) {
  const host = document.createElement('main'); document.body.append(host);
  const dock = createAgentSidebarDock({ host, translate }); dock.update('one', items);
  const row = () => host.querySelector<HTMLDetailsElement>('.agent-dock-agent')!;
  const open = async () => { row().open = true; row().dispatchEvent(new Event('toggle')); await settle(); };
  return { host, dock, row, open };
}
afterEach(() => document.body.replaceChildren());
describe('background agent dock', () => {

  it('keeps task cards primary and scopes folded messages to exact run participants without settling work', async () => {
    const agent = { ...item('a'), agentId: 'worker', swarmRunId: 'run' };
    const { host, dock, row, open } = setup([agent, item('failed', 'failed')]);
    const receipt = { swarmRunId: 'run', agentId: 'worker', messageId: 'm', state: 'input_confirmed', content: 'Full message' };
    const run = { swarm_id: 'run', messageReceiptsAvailable: true, messageReceipts: [receipt,
      { ...receipt, agentId: 'other', messageId: 'other', content: 'Other task' }], messageParticipants: [{ id: 'worker', label: 'Worker' }] };
    dock.update('one', [agent, item('failed', 'failed')], [run]);
    const all = host.querySelector<HTMLDetailsElement>('.agent-dock-messages')!;
    expect(all.open).toBe(false);
    expect(all.previousElementSibling?.className).toBe('agent-dock-history');
    expect(row().dataset.status).toBe('running');
    expect(host.querySelector('[data-status="failed"]')).not.toBeNull();
    await open();
    const messages = row().querySelector<HTMLDetailsElement>('.agent-dock-agent-messages')!;
    expect(messages.open).toBe(false); messages.open = true;
    expect(messages.querySelector('select')!.closest('[hidden]')).not.toBeNull();
    expect(messages.textContent).toContain('Full message'); expect(messages.textContent).not.toContain('Other task');
    const body = messages.querySelector<HTMLDetailsElement>('.sw-messages-row > details')!;
    body.open = true; const summary = body.querySelector('summary')!; summary.focus();
    dock.update('one', [agent, item('failed', 'failed')], [{ ...run, messageReceipts: [{ ...receipt, content: 'Updated full message' }] }]);
    expect(messages.open).toBe(true); expect(body.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(body.textContent).toContain('Updated full message'); expect(row().dataset.status).toBe('running');
    dock.update('two', [], []); expect(all.open).toBe(false); expect(host.querySelector('.sw-messages-row')).toBeNull();
  });

  it('shares the stable turn info column through context repaint without changing chat width', async () => {
    const container = document.createElement('div'); document.body.append(container);
    const vm = (model: string): any => ({ conversationId:'one', conversationRevision:model.length, transport:'live', orphanLanes:[], queue:[],
      mainLane:{laneId:'main', parentTurnId:null, title:'Conversation', kind:'main', turns:[{
        turnId:'human',laneId:'main',parentTurnId:null,ordinal:1,actor:'human',role:'user',kind:'input',status:'completed',
        attemptId:null,projectionRevision:model.length,subtreeRevisionKey:model,commandPending:null,finish:null,branches:[],actions:[],metadata:{},source:{},
        blocks:[{kind:'context',blockId:'ctx',identitySource:'contract',source:{model},value:{snapshot:{model}}}],
      }]}});
    const surface = createConversationSurface(container, { renderTurnContextRail(node, block) {
      node.replaceChildren(); node.textContent = String(block?.value.snapshot.model || ''); node.hidden = !block;
    } });
    surface.render(vm('first'));
    const rail = backgroundAgentContextHost(container, 'one', 'human')!;
    expect(rail.dataset.conversationPart).toBe('turn-context-rail');
    const agent = item('a'); const dock = createAgentSidebarDock({host:rail,translate}); dock.update('one',[agent]);
    const row = rail.querySelector<HTMLDetailsElement>('.agent-dock-agent')!;
    row.open = true; row.dispatchEvent(new Event('toggle')); await settle();
    const summary = row.querySelector('summary')!; summary.focus();
    surface.render(vm('new-model'));
    expect(backgroundAgentContextHost(container,'one','human')).toBe(rail);
    expect(rail.querySelector('.agent-dock-agent')).toBe(row);
    expect(row.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(rail.textContent).toContain('new-model'); expect(agent.render).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.has-agent-dock')).toBeNull();
    expect(backgroundAgentContextHost(container,'other','human')).toBeNull();
    expect(backgroundAgentContextHost(container,'one','unmounted')).toBeNull();
    dock.mount(null); expect(container.querySelector('.agent-dock')).toBeNull();
    dock.mount(rail); expect(rail.querySelector('.agent-dock-agent')).toBe(row);
    dock.dispose(); surface.dispose();
  });
  it('starts collapsed and renders execution only on explicit expansion', async () => {
    const agent = item('a'); const { host, open } = setup([agent]);
    expect(host.querySelector('.agent-dock')!.classList.contains('is-expanded')).toBe(false);
    expect(agent.render).not.toHaveBeenCalled(); await open();
    expect(agent.render).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain('read_files');
    expect(host.classList.contains('has-expanded-agent-dock')).toBe(false);
    expect(host.querySelector('.agent-dock-avatar')).toBeNull();
  });
  it('keeps readable status and count without requesting execution details', () => {
    const agent = { ...item('a'), statusLabel: 'Running · round 42' };
    const { host, dock } = setup([agent, item('b', 'pending')]);
    expect(host.querySelector('.agent-dock-counts')!.textContent).toBe('1 running · 1 waiting');
    expect(host.querySelector('.agent-dock-status')!.textContent).toBe('Running · round 42');
    expect(agent.render).not.toHaveBeenCalled();
    dock.update('two', [{ ...item('new'), statusLabel: 'Running · round 3' }]);
    expect(host.textContent).not.toContain('round 42');
    expect(host.textContent).toContain('round 3');
  });
  it('retains an open completed row until the reader closes it', async () => {
    const agent = item('a'); const { host, dock, row, open } = setup([agent]); await open();
    const shell = row(); const nested = host.querySelector<HTMLDetailsElement>('.agent-dock-detail details')!;
    nested.open = true; shell.querySelector('summary')!.focus();
    dock.update('one', [{ ...agent, status: 'done' }]); await settle();
    expect(row()).toBe(shell); expect(shell.open).toBe(true); expect(nested.open).toBe(true);
    expect(document.activeElement).toBe(shell.querySelector('summary'));
    shell.open = false; shell.dispatchEvent(new Event('toggle'));
    expect(host.querySelectorAll('.agent-dock-active .agent-dock-agent')).toHaveLength(0);
    expect(host.querySelector('.agent-dock-history')!.textContent).toContain('Finished history · 1');
  });
  it('preserves shell, nested fold and focused summary through unchanged snapshots', async () => {
    const agent = item('a'); const { host, dock, row, open } = setup([agent]); await open();
    const shell = row(); const nested = host.querySelector<HTMLDetailsElement>('.agent-dock-detail details')!;
    nested.open = true; const summary = shell.querySelector('summary')!; summary.focus();
    dock.update('one', [{ ...agent }]); await settle();
    expect(row()).toBe(shell); expect(nested.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(agent.render).toHaveBeenCalledTimes(1);
  });
  it.each(['producerTask', 'workerTask'] as const)('hydrates a unique missing %s without losing folds, focus or current callbacks', async stamp => {
    const agent = { ...execution('missing', 'producer', 'worker'), openConversation: vi.fn() };
    agent.identity![stamp] = '';
    const { host, dock, row, open } = setup([agent]); await open();
    const shell = row();
    const nested = shell.querySelector<HTMLDetailsElement>('[data-rich-key="call-1"]')!;
    nested.open = true; const summary = nested.querySelector('summary')!; summary.focus();
    const hydrated = { ...execution('known', 'producer', 'worker'), openConversation: vi.fn(),
      render: vi.fn(async () => '<details data-rich-key="call-1"><summary>read_files</summary><pre>hydrated output</pre></details>') };
    dock.update('one', [hydrated]); await settle();
    expect(row()).toBe(shell); expect(shell.dataset.agentKey).toBe('known'); expect(shell.open).toBe(true);
    expect(shell.querySelector('[data-rich-key="call-1"]')).toBe(nested); expect(nested.open).toBe(true);
    expect(document.activeElement).toBe(summary); expect(nested.textContent).toContain('hydrated output');
    expect(hydrated.render).toHaveBeenCalledTimes(1);
    shell.querySelector<HTMLButtonElement>('button')!.click();
    expect(hydrated.openConversation).toHaveBeenCalledTimes(1); expect(agent.openConversation).not.toHaveBeenCalled();
    shell.open = false; shell.dispatchEvent(new Event('toggle'));
    const refreshed = { ...hydrated, revision: '2', render: vi.fn(async () => '<pre>current callback</pre>') };
    dock.update('one', [refreshed]); await settle(); expect(refreshed.render).not.toHaveBeenCalled();
    shell.open = true; shell.dispatchEvent(new Event('toggle')); await settle();
    expect(refreshed.render).toHaveBeenCalledTimes(1); expect(host.textContent).toContain('current callback');
  });
  it('adds and removes a hydrated conversation action without replacing the retained row', async () => {
    const { dock, row, open } = setup([execution('missing')]); await open(); const shell = row();
    const summary = shell.querySelector('summary')!; summary.focus();
    expect(shell.querySelector('button')).toBeNull();
    const hydrated = { ...execution('known', 'producer', 'worker'), openConversation: vi.fn() };
    dock.update('one', [hydrated]); await settle();
    expect(row()).toBe(shell); expect(shell.open).toBe(true); expect(document.activeElement).toBe(summary);
    const action = shell.querySelector<HTMLButtonElement>('button')!;
    expect(action.textContent).toBe('Open agent conversation'); action.click();
    expect(hydrated.openConversation).toHaveBeenCalledTimes(1);
    dock.update('one', [{ ...hydrated, openConversation: undefined }]); await settle();
    expect(row()).toBe(shell); expect(shell.querySelector('button')).toBeNull();
    action.click(); expect(hydrated.openConversation).toHaveBeenCalledTimes(1);

    dock.update('one', [hydrated]); await settle();
    action.click(); expect(hydrated.openConversation).toHaveBeenCalledTimes(1);
    shell.querySelector<HTMLButtonElement>('button')!.click();
    expect(hydrated.openConversation).toHaveBeenCalledTimes(2);
  });
  it('retains an open row hydrated at completion until its current-key toggle closes it', async () => {
    const { host, dock, row, open } = setup([execution('missing')]); await open(); const shell = row();
    dock.update('one', [{ ...execution('known', 'producer', 'worker'), status: 'done' }]); await settle();
    expect(row()).toBe(shell); expect(shell.open).toBe(true);
    expect(shell.parentElement?.className).toBe('agent-dock-active');
    shell.open = false; shell.dispatchEvent(new Event('toggle'));
    expect(host.querySelector('.agent-dock-agent')).toBeNull();
    expect(host.querySelector('.agent-dock-history')!.textContent).toContain('Finished history · 1');
  });
  it.each(['producerTask', 'workerTask', 'scope'] as const)('never reuses a row across explicit %s conflict, even for an exact key', async stamp => {
    const agent = execution('known', 'producer-one', 'worker-one');
    const { dock, row, open } = setup([agent]); await open(); const shell = row();
    const replacement = { ...agent, identity: { ...agent.identity!, [stamp]: 'different-execution' } };
    dock.update('one', [replacement]); await settle();
    expect(row()).not.toBe(shell); expect(shell.isConnected).toBe(false); expect(row().open).toBe(false);
  });
  it('replaces worker-one DOM with worker-two rather than treating the change as hydration', async () => {
    const { dock, row, open } = setup([execution('worker-one', 'producer', 'worker-one')]); await open();
    const shell = row(); dock.update('one', [execution('worker-two', 'producer', 'worker-two')]); await settle();
    expect(row()).not.toBe(shell); expect(shell.isConnected).toBe(false); expect(row().open).toBe(false);
  });
  it('keeps the exact worker-one row when worker-two is appended', async () => {
    const first = execution('worker-one', 'producer', 'worker-one');
    const { host, dock, row, open } = setup([first]); await open(); const shell = row();
    const summary = shell.querySelector('summary')!; summary.focus();
    dock.update('one', [{ ...first }, execution('worker-two', 'producer', 'worker-two')]); await settle();
    expect(host.querySelectorAll('.agent-dock-agent')).toHaveLength(2); expect(row()).toBe(shell);
    expect(shell.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(first.render).toHaveBeenCalledTimes(1);
  });
  it('does not guess which known execution inherits an ambiguous unknown row', async () => {
    const { host, dock, row, open } = setup([execution('missing')]); await open(); const shell = row();
    dock.update('one', [execution('worker-one', 'producer', 'worker-one'), execution('worker-two', 'producer', 'worker-two')]); await settle();
    const incoming = [...host.querySelectorAll<HTMLDetailsElement>('.agent-dock-agent')];
    expect(incoming).toHaveLength(2); expect(incoming).not.toContain(shell);
    expect(incoming.every(node => !node.open)).toBe(true); expect(shell.isConnected).toBe(false);
  });
  it('requires a unique previous row as well as a unique incoming candidate', async () => {
    const { dock, row, open } = setup([execution('missing-one'), execution('missing-two')]); await open();
    const shell = row(); dock.update('one', [execution('known', 'producer', 'worker')]); await settle();
    expect(row()).not.toBe(shell); expect(row().open).toBe(false); expect(shell.isConnected).toBe(false);
  });
  it('does not remap legacy rows without explicit identity evidence', async () => {
    const { dock, row, open } = setup([item('missing')]); await open(); const shell = row();
    dock.update('one', [execution('known', 'producer', 'worker')]); await settle();
    expect(row()).not.toBe(shell); expect(row().open).toBe(false);
  });
  it('does not forget known stamps when an intermediate snapshot has missing stamps', async () => {
    const { dock, row, open } = setup([execution('worker-one', 'producer', 'worker-one')]); await open();
    const shell = row(); dock.update('one', [execution('missing')]); await settle(); expect(row()).toBe(shell);
    dock.update('one', [execution('worker-two', 'producer', 'worker-two')]); await settle();
    expect(row()).not.toBe(shell); expect(shell.isConnected).toBe(false);
  });
  it.each(['resolve', 'reject'] as const)('fences a stale in-flight %s and serializes a single latest hydration refresh', async outcome => {
    let resolve!: (html: string) => void; let reject!: (error: Error) => void;
    const slow = execution('missing', '', '');
    slow.render = vi.fn(() => new Promise<string>((done, fail) => { resolve = done; reject = fail; }));
    const { host, dock, row, open } = setup([slow]); await open(); const shell = row();
    const partial = execution('producer-known');
    let resolveCurrent!: (html: string) => void;
    const hydrated = { ...execution('known', 'producer', 'worker'),
      render: vi.fn(() => new Promise<string>(done => { resolveCurrent = done; })) };
    dock.update('one', [partial]); dock.update('one', [hydrated]); dock.update('one', [{ ...hydrated }]);
    expect(row()).toBe(shell); expect(partial.render).not.toHaveBeenCalled(); expect(hydrated.render).not.toHaveBeenCalled();
    if (outcome === 'resolve') resolve('<pre>STALE DETAILS</pre>'); else reject(new Error('stale failure'));
    await settle();
    expect(row()).toBe(shell); expect(hydrated.render).toHaveBeenCalledTimes(1);
    expect(partial.render).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain('STALE DETAILS'); expect(host.textContent).not.toContain('could not be loaded');
    resolveCurrent('<pre>latest hydrated details</pre>'); await settle();
    expect(host.textContent).toContain('latest hydrated details'); expect(hydrated.render).toHaveBeenCalledTimes(1);
  });
  it('keeps a failed detail reader-driven through unchanged snapshots', async () => {
    const failed = { ...execution('known', 'producer', 'worker'), render: vi.fn(async () => { throw new Error('unavailable'); }) };
    const { dock, row, open } = setup([failed]); await open();
    const retry = row().querySelector('.agent-dock-detail button');
    dock.update('one', [{ ...failed }]); await settle();
    expect(failed.render).toHaveBeenCalledTimes(1); expect(row().querySelector('.agent-dock-detail button')).toBe(retry);
  });
  it('retries through the current row key after hydration', async () => {
    const failed = { ...execution('missing'), render: vi.fn(async () => { throw new Error('unavailable'); }) };
    const { host, dock, row, open } = setup([failed]); await open(); const shell = row();
    const retry = shell.querySelector<HTMLButtonElement>('.agent-dock-detail button')!;
    shell.open = false; shell.dispatchEvent(new Event('toggle'));
    const hydrated = { ...execution('known', 'producer', 'worker'), render: vi.fn(async () => '<pre>retry current execution</pre>') };
    dock.update('one', [hydrated]); expect(row()).toBe(shell);
    shell.open = true; retry.click(); await settle();
    expect(hydrated.render).toHaveBeenCalledTimes(1); expect(failed.render).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain('retry current execution');
  });
  it('patches changed canonical records without replacing keyed tool disclosures', async () => {
    const agent = item('a'); const { host, dock, open } = setup([agent]); await open();
    const nested = host.querySelector<HTMLDetailsElement>('[data-rich-key="call-1"]')!; nested.open = true;
    dock.update('one', [{ ...agent, revision: '2', render: async () => '<details data-rich-key="call-1"><summary>read_files</summary><pre>new output</pre></details>' }]); await settle();
    expect(host.querySelector('[data-rich-key="call-1"]')).toBe(nested);
    expect(nested.open).toBe(true); expect(nested.textContent).toContain('new output');
  });
  it('tucks only evidenced success/cancellation into demand-paged history', async () => {
    const { host, dock } = setup([item('success', 'done'), item('failure', 'failed'), item('lost', 'unknown')]);
    expect(host.querySelectorAll('.agent-dock-active .agent-dock-agent')).toHaveLength(2);
    expect(host.querySelectorAll('.agent-dock-history .agent-dock-agent')).toHaveLength(0);
    const history = host.querySelector<HTMLDetailsElement>('.agent-dock-history')!; history.open = true; history.dispatchEvent(new Event('toggle'));
    expect(history.querySelectorAll('.agent-dock-agent')).toHaveLength(1);
    dock.update('one', [item('failure', 'failed')]);
    expect(host.querySelector('.agent-dock-active')!.textContent).toContain('failure');
    expect(history.hidden).toBe(true); // disappearance is not promoted into success history
  });
  it('mounts 20 history rows until reader explicitly requests more', () => {
    const { host } = setup(Array.from({ length: 45 }, (_, i) => item(String(i), 'done')));
    const history = host.querySelector<HTMLDetailsElement>('.agent-dock-history')!;
    history.open = true; history.dispatchEvent(new Event('toggle'));
    expect(history.querySelectorAll('.agent-dock-agent')).toHaveLength(20);
    history.querySelector<HTMLButtonElement>('button')!.click();
    expect(history.querySelectorAll('.agent-dock-agent')).toHaveLength(40);
  });
  it('fences a late execution response after conversation switch', async () => {
    let resolve!: (html: string) => void;
    const slow = item('old'); slow.render = () => new Promise<string>(done => { resolve = done; });
    const { host, dock, open } = setup([slow]); await open();
    dock.update('two', [item('new')]); resolve('<pre>FOREIGN RECORD</pre>'); await settle();
    expect(host.textContent).not.toContain('FOREIGN RECORD'); expect(host.textContent).toContain('new');
  });
  it('does not commit late details into a retired context host', async () => {
    let resolve!: (html: string) => void;
    const slow = item('old'); slow.render = () => new Promise<string>(done => { resolve = done; });
    const { host, dock, open } = setup([slow]); await open();
    host.remove(); resolve('<pre>RETIRED HOST RECORD</pre>'); await settle();
    expect(host.textContent).not.toContain('RETIRED HOST RECORD');
    dock.dispose();
  });
  it('fences disposal and never starts another pending render', async () => {
    let resolve!: (html: string) => void;
    const slow = item('old'); slow.render = () => new Promise<string>(done => { resolve = done; });
    const { host, dock, open } = setup([slow]); await open(); dock.dispose(); resolve('<pre>late</pre>'); await settle();
    expect(host.children).toHaveLength(0); expect(host.classList.contains('has-agent-dock')).toBe(false);
  });
  it('Escape closes execution detail and focuses its existing summary', async () => {
    const { host, row, open } = setup([item('a')]); await open();
    row().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(row().open).toBe(false);
    expect(host.querySelector('.agent-dock')!.classList.contains('is-expanded')).toBe(false);
    expect(document.activeElement).toBe(row().querySelector('summary'));
  });
  it('serializes lazy rendering and retains one trailing demand', async () => {
    let resolve!: (html: string) => void;
    const first = item('first'); const second = item('second');
    first.render = vi.fn(() => new Promise<string>(done => { resolve = done; }));
    const { host } = setup([first, second]);
    const rows = host.querySelectorAll<HTMLDetailsElement>('.agent-dock-agent');
    rows[0].open = true; rows[0].dispatchEvent(new Event('toggle'));
    rows[1].open = true; rows[1].dispatchEvent(new Event('toggle'));
    expect(second.render).not.toHaveBeenCalled(); resolve('<pre>first</pre>'); await settle();
    expect(second.render).toHaveBeenCalledTimes(1);
  });
});
