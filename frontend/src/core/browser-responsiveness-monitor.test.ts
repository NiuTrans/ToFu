import { describe, expect, it } from 'vitest';
import { responsivenessContext, startBrowserResponsivenessMonitor, type ResponsivenessEntry } from './browser-responsiveness-monitor';
import { createTurnState } from './turn-state';

function harness() {
  let now = 1000;
  const callbacks = new Map<string, (entries: ResponsivenessEntry[]) => void>();
  const messages: string[] = [];
  let listener: (() => void) | null = null;
  const visibility = { hidden: false,
    addEventListener: (_: string, callback: unknown) => { listener = callback as () => void; },
    removeEventListener: () => { listener = null; } };
  const owner = startBrowserResponsivenessMonitor({
    timeOriginMs: 100000, now: () => now, visibility: visibility as unknown as Document,
    context: () => ({ conversationId: 'conv-test' }), report: message => messages.push(message),
    observe: (type, callback) => { callbacks.set(type, callback); return () => { callbacks.delete(type); }; },
  });
  const send = (duration = 400, extras = {}) => callbacks.get('longtask')?.([
    { entryType: 'longtask', duration, startTime: 100, ...extras },
  ]);
  return { owner, messages, callbacks, visibility, send,
    time: (value: number) => { now = value; }, change: () => listener?.() };
}

describe('passive stall diagnostics', () => {
  it('reports meaningful recovered stalls, coalesces bursts and retains the maximum', () => {
    const h = harness();
    h.send(249); expect(h.messages).toHaveLength(0);
    h.send(); h.send(2000); h.send(500);
    expect(h.messages).toHaveLength(1);
    h.time(6000); h.send();
    expect(h.messages[1]).toContain('"suppressed":2');
    expect(h.messages[1]).toContain('"suppressedMaximumMs":2000');
    expect(h.messages[0]).toContain('"conversationId":"conv-test"');
    expect(h.messages[0]).toContain('"observedDelayMs":500');
    expect(h.messages[0]).toContain('"startAtMs":100100');
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
        {sourceURL:'https://example.test/assets/a.js?secret=private#token',duration:500,forcedStyleAndLayoutDuration:200},
        {sourceURL:'https://example.test/other.js',duration:50},
      ] }]);
    expect(h.messages[0]).toContain('/assets/a.js');
    expect(h.messages[0]).toContain('"layoutMs":200');
    expect(h.messages[0]).not.toMatch(/secret|private|token|other/);
  });
  it('degrades safely when unsupported or when reporting fails', () => {
    const h = harness(); h.owner.dispose();
    const owner = startBrowserResponsivenessMonitor({ now:()=>0, visibility:h.visibility as unknown as Document,
      observe:()=> { throw Error('unsupported'); }, context:()=>({}), report:()=>{throw Error('offline');} });
    expect(()=>owner.dispose()).not.toThrow();
    for (const failingPort of ['context', 'report']) {
      let receive!: (entries: ResponsivenessEntry[]) => void;
      const failing = startBrowserResponsivenessMonitor({ now:()=>1000, visibility:h.visibility as unknown as Document,
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
      context:()=>context, report:message=>messages.push(message),
      observe:(_type, callback)=>{ receive=callback;return()=>{}; } });
    receive([{entryType:'long-animation-frame',duration:400,startTime:0,blockingDuration:350,
      scripts:[{sourceURL:'https://example.test/'+'x'.repeat(160),sourceFunctionName:'f'.repeat(60),duration:300}]}]);
    expect(messages[0].length).toBeLessThanOrEqual(800);
    expect(JSON.parse(messages[0].split('] ')[1]).toolCallId).toBe('x'.repeat(64));
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
