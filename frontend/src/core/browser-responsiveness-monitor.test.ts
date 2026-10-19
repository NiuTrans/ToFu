import { describe, expect, it } from 'vitest';
import { responsivenessContext, startBrowserResponsivenessMonitor, type ResponsivenessEntry } from './browser-responsiveness-monitor';
import { createTurnState } from './turn-state';

function harness() {
  let now = 1000;
  const callbacks = new Map<string, (entries: ResponsivenessEntry[]) => void>();
  const messages: string[] = [];
  const timers = new Map<number, () => void>();
  let timerId = 0;
  const schedule = { setTimeout: (callback: () => void) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id: unknown) => { timers.delete(id as number); } };
  const queued = new Map<string, ResponsivenessEntry[]>();
  const lifecycleListeners = new Map<string, EventListener>();
  const lifecycle = {
    addEventListener: (type: string, callback: EventListener) => { lifecycleListeners.set(type, callback); },
    removeEventListener: (type: string) => { lifecycleListeners.delete(type); },
  } as unknown as Window;
  let conversationId = 'conv-test';
  let listener: (() => void) | null = null;
  const visibility = { hidden: false,
    addEventListener: (_: string, callback: unknown) => { listener = callback as () => void; },
    removeEventListener: () => { listener = null; } };
  const owner = startBrowserResponsivenessMonitor({
    timeOriginMs: 100000, now: () => now, visibility: visibility as unknown as Document,
    schedule, lifecycle, wallNow: () => 123456,
    context: () => ({ conversationId }), report: message => messages.push(message),
    observe: (type, callback) => { callbacks.set(type, callback); return {
      disconnect: () => { callbacks.delete(type); },
      takeRecords: () => queued.get(type) ?? [],
    }; },
  });
  const send = (duration = 400, extras = {}) => callbacks.get('longtask')?.([
    { entryType: 'longtask', duration, startTime: 100, ...extras },
  ]);
  return { owner, messages, callbacks, visibility, send, schedule, timers, queued, lifecycleListeners,
    conversation: (value: string) => { conversationId = value; },
    runTimer: () => { const fn = timers.values().next().value; timers.clear(); fn?.(); },
    time: (value: number) => { now = value; }, change: () => listener?.() };
}

describe('passive stall diagnostics', () => {
  it('reports meaningful recovered stalls, coalesces bursts and retains the maximum', () => {
    const h = harness();
    h.send(249); expect(h.messages).toHaveLength(0);
    h.send(); h.send(700); h.send(500);
    expect(h.messages).toHaveLength(1);
    expect(h.timers.size).toBe(1);
    h.time(6000); h.runTimer();
    expect(h.messages[1]).toContain('"suppressed":2');
    expect(h.messages[1]).toContain('"durationMs":700');
    expect(h.timers.size).toBe(0);
    expect(h.messages[0]).toContain('"conversationId":"conv-test"');
    expect(h.messages[0]).toContain('"observedDelayMs":500');
    expect(h.messages[0]).toContain('"startAtMs":100100');
    expect(h.messages[0]).toContain('"observedAtMs":123456');
  });
  it('reports the longest recovered entry first and promotes a later severe stall', () => {
    const h = harness();
    h.callbacks.get('longtask')!([
      { entryType: 'longtask', startTime: 0, duration: 300 },
      { entryType: 'longtask', startTime: 300, duration: 20000 },
    ]);
    expect(h.messages).toHaveLength(1);
    expect(h.messages[0]).toContain('"durationMs":20000');
    expect(h.messages[0]).toContain('"suppressed":1');
    h.owner.dispose();
    const next = harness();
    next.send(300); next.send(1500); next.send(20000);
    expect(next.messages).toHaveLength(2);
    expect(next.messages[1]).toContain('"durationMs":1500');
    next.visibility.hidden = true; next.change();
    expect(next.messages[2]).toContain('"durationMs":20000');
    expect(next.timers.size).toBe(0);
  });
  it('drains completed observer entries and suppressed evidence on hide and disposal', () => {
    const h = harness(); h.send(300); h.send(600);
    h.queued.set('longtask', [{ entryType: 'longtask', startTime: 900, duration: 20000 }]);
    h.visibility.hidden = true; h.change();
    expect(h.messages.some(line => line.includes('"durationMs":20000'))).toBe(true);
    expect(h.timers.size).toBe(0);
    h.queued.clear(); h.visibility.hidden = false; h.change();
    h.send(300); h.send(800); h.owner.dispose();
    expect(h.messages.at(-1)).toContain('"durationMs":800');
    expect(h.callbacks.size).toBe(0); expect(h.timers.size).toBe(0);
  });
  it('preserves original context and drains on pagehide, then resumes without stale callbacks', () => {
    const h = harness(); const late = h.callbacks.get('longtask')!;
    h.send(300); h.send(800); h.conversation('new-conversation');
    h.lifecycleListeners.get('pagehide')!(new Event('pagehide'));
    expect(JSON.parse(h.messages.at(-1)!.split('] ')[1])).toMatchObject({
      conversationId: 'conv-test', durationMs: 800,
    });
    expect(h.callbacks.size).toBe(0); expect(h.timers.size).toBe(0);
    h.lifecycleListeners.get('pageshow')!(new Event('pageshow'));
    late([{ entryType: 'longtask', duration: 20000, startTime: 0 }]);
    expect(h.messages).toHaveLength(2);
    h.send(300);
    expect(h.messages.at(-1)).toContain('new-conversation');
    h.owner.dispose(); expect(h.lifecycleListeners.size).toBe(0);
  });
  it('disconnects while hidden and on repeated disposal; ignores late deliveries', () => {
    const h = harness(); const late = h.callbacks.get('longtask')!;
    h.visibility.hidden = true; h.change();
    expect(h.callbacks.size).toBe(0); late([{entryType:'longtask',duration:400,startTime:0}]);
    expect(h.messages).toHaveLength(0);
    h.visibility.hidden = false; h.change(); expect(h.callbacks.size).toBe(2);
    late([{entryType:'longtask',duration:400,startTime:0}]);
    expect(h.messages).toHaveLength(0);
    h.owner.dispose(); h.owner.dispose(); late([{entryType:'longtask',duration:400,startTime:0}]);
    expect(h.callbacks.size).toBe(0); expect(h.messages).toHaveLength(0);
  });
  it('attributes only the slowest script and strips query strings and fragments', () => {
    const h = harness();
    h.callbacks.get('long-animation-frame')!([{ entryType:'long-animation-frame', duration:600,
      startTime:10, blockingDuration:550, scripts:[
        {sourceURL:'https://example.test/assets/a.js?secret=private#token',duration:500,forcedStyleAndLayoutDuration:200,
          sourceCharPosition: 12345, invokerType: 'event-listener'},
        {sourceURL:'https://example.test/other.js',duration:50},
      ] }]);
    expect(h.messages[0]).toContain('/assets/a.js');
    expect(h.messages[0]).toContain('"layoutMs":200');
    expect(h.messages[0]).toContain('"charPosition":12345');
    expect(h.messages[0]).toContain('"invokerType":"event-listener"');
    expect(h.messages[0]).not.toMatch(/secret|private|token|other/);
  });
  it('degrades safely when unsupported or when reporting fails', () => {
    const h = harness(); h.owner.dispose();
    const owner = startBrowserResponsivenessMonitor({ now:()=>0, visibility:h.visibility as unknown as Document,
      schedule: h.schedule,
      observe:()=> { throw Error('unsupported'); }, context:()=>({}), report:()=>{throw Error('offline');} });
    expect(()=>owner.dispose()).not.toThrow();
    for (const failingPort of ['context', 'report']) {
      let receive!: (entries: ResponsivenessEntry[]) => void;
      const failing = startBrowserResponsivenessMonitor({ now:()=>1000, visibility:h.visibility as unknown as Document,
        schedule: h.schedule,
        observe:(_type, callback)=>{receive=callback;return()=>{};},
        context:()=>{if(failingPort==='context')throw Error('unavailable');return {};},
        report:()=>{throw Error('offline');} });
      expect(()=>receive([{entryType:'longtask',duration:400,startTime:0}])).not.toThrow();
      failing.dispose();
    }
  });
  it('keeps bounded identifier evidence parseable through the 800-character relay cap', () => {
    const h = harness(); h.owner.dispose();
    const messages: string[] = [];
    let receive!: (entries: ResponsivenessEntry[]) => void;
    const context = Object.fromEntries(['conversationId','turnId','attemptId','taskId','tool','toolCallId','toolStatus','transport']
      .map(key => [key, 'x'.repeat(64)]));
    const owner = startBrowserResponsivenessMonitor({ now:()=>1000, visibility:h.visibility as unknown as Document,
      schedule: h.schedule,
      context:()=>context, report:message=>messages.push(message),
      observe:(_type, callback)=>{ receive=callback;return()=>{}; } });
    receive([{entryType:'long-animation-frame',duration:400,startTime:0,blockingDuration:350,
      scripts:[{sourceURL:'https://example.test/'+'x'.repeat(160),sourceFunctionName:'f'.repeat(60),duration:300}]}]);
    expect(messages[0].length).toBeLessThanOrEqual(800);
    const evidence = JSON.parse(messages[0].split('] ')[1]);
    expect(evidence.script.path).toBe('x'.repeat(120));
    expect(evidence.conversationId).toBe('x'.repeat(64));
    owner.dispose();
  });
  it('reads canonical identifiers without including arguments or tool output', () => {
    const state = createTurnState('conv');
    state.laneOrder.main = ['turn'];
    state.turnsById.turn = {turnId:'turn', currentAttemptId:'attempt', projection:{toolRounds:[{
      toolName:'exec', toolCallId:'call', status:'searching', toolArgs:'secret',toolContent:'private',
    }]}} as never;
    state.attemptsById.attempt = {attemptId:'attempt',taskId:'task'};
    const context = responsivenessContext('conv',state);
    expect(context).toMatchObject({conversationId:'conv',attemptId:'attempt',taskId:'task',tool:'exec',toolCallId:'call'});
    expect(JSON.stringify(context)).not.toMatch(/secret|private/);
    expect(()=>responsivenessContext(null,null)).not.toThrow();
  });
});
