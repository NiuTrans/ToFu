"""DIAGNOSTIC (throwaway): replay the REAL recorded turn-16 event stream
(conversation mttwbp91mzuqd8, turn 770f2865, regenerate attempt revs 74..790)
through the live app's own ConversationTurnStore with autoTranslate ON, and
count, after every applied event, how many .md-content nodes in the rendered
DOM carry the narration needle. A count > 1 is the user-visible duplicate.

The recorded stream already contains the translation stamps as data, so no
live translation is needed; the only stub is a trivial LLM that creates a
host conversation.
"""
from __future__ import annotations

import json
import os
import time

import pytest

pytestmark = [pytest.mark.visual]

_FINAL_TEXT = 'replay host conversation settled'


def _stub_stream_llm_response(task, body, tag='', on_tool_call_ready=None):
    with task['content_lock']:
        task['content'] += _FINAL_TEXT
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager._events import append_event
    append_event(task, build_event(EventType.DELTA, content=_FINAL_TEXT))
    return ({'role': 'assistant', 'content': _FINAL_TEXT, 'tool_calls': []},
            'stop',
            {'prompt_tokens': 5, 'completion_tokens': 5, 'total_tokens': 10})


@pytest.fixture(scope='module', autouse=True)
def _install_llm_stubs():
    import lib.tasks_pkg.llm_fallback._call as llm_fb

    saved = llm_fb.stream_llm_response
    llm_fb.stream_llm_response = _stub_stream_llm_response
    try:
        yield
    finally:
        llm_fb.stream_llm_response = saved


def _wait_app_ready(page, timeout=15000):
    page.wait_for_selector('#userInput', state='visible', timeout=timeout)
    page.wait_for_function(
        "window.TofuModules && window.TofuModules.version === 3",
        timeout=timeout)


_REPLAY_JS = r"""
async ({ events, seedProjection, turnId, needle, convId }) => {
  const attempt1 = events.find((e) => e.projectionRevision < 74).attemptId;
  const conv = (typeof conversations !== 'undefined' ? conversations : [])
    .find((c) => c && c.id === convId);
  if (conv) conv.autoTranslate = true;
  const store = globalThis.ConversationTurnStore.ensureRuntimeStore(convId);
  const seeded = {
    turnId,
    conversationId: convId,
    laneId: 'main',
    actor: 'assistant',
    kind: 'message',
    status: 'interrupted',
    currentAttemptId: attempt1,
    projection: seedProjection,
    projectionRevision: 73,
    ordinal: 99,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  };
  store.dispatch({ type: 'snapshot',
                   snapshot: { conversationRevision: 73, turns: [seeded] } });
  const sleep = () => new Promise(
    (r) => requestAnimationFrame(() => setTimeout(r, 0)));
  await sleep();

  const describe = (n) => {
    const sec = n.closest('[data-block-id]');
    let p = n; const chain = [];
    for (let i = 0; i < 6 && p; i += 1) {
      chain.push(`${p.tagName.toLowerCase()}.${
        String(p.className || '').split(' ')[0]}${
        p.dataset?.blockId ? '#' + p.dataset.blockId : ''}`);
      p = p.parentElement;
    }
    return {
      bid: sec?.dataset.blockId || '',
      kind: sec?.dataset.blockKind || '',
      cls: n.className,
      html: n.outerHTML.slice(0, 220),
      chain,
    };
  };

  const report = [];
  let dupAt = null;
  let maxN = 0;
  let snapCount = 0;
  let translateCount = 0;
  for (const ev of events) {
    ev.conversationId = convId;
    if (ev.payload && ev.payload.turnState) {
      ev.payload.turnState.conversationId = convId;
    }
    store.dispatch({ type: 'event', event: ev });
    // Mid-stream inputs the user's real session had and the plain replay
    // lacked: (1) a full authoritative snapshot landing right after the
    // narration materialised (13:57:10 sync fetch), (2) translate 'running'
    // frames carrying a partialByRound preview for the thinking segment
    // (13:57:09-13:57:40 incremental window).
    if (ev.projectionRevision === 225) {
      const st = store.getState().turnsById[turnId];
      const snap = {
        conversationId: convId,
        conversationRevision: st.projectionRevision,
        turns: [JSON.parse(JSON.stringify({
          ...st, presentationId: turnId,
        }))],
        attempts: [], queueItems: [],
      };
      store.dispatch({ type: 'snapshot', snapshot: snap,
                       authoritativeFull: true });
      snapCount += 1;
    }
    if (ev.projectionRevision === 226 || ev.projectionRevision === 228) {
      const acts = globalThis.ConversationSurfacePresentation;
      if (acts?.setTranslationActivity) {
        acts.setTranslationActivity(convId, turnId, {
          status: 'pending', message: 'Translating…',
          partialByRound: new Map([[
            'attempt-' + attempt1 + ':llm-0', 'partial thinking preview']]),
        });
        translateCount += 1;
      }
    }
    await sleep();
    if (ev.projectionRevision >= 210 && ev.projectionRevision <= 230) {
      const st = store.getState().turnsById[turnId];
      const p = st?.projection || {};
      report.push({
        rev: ev.projectionRevision, inv: true,
        contentLen: (p.content || '').length,
        segs: (p.segments || []).map((s) => (
          `${s.blockId}:${s.type}${s.terminal ? 'T' : ''}${s.deliverable ? 'D' : ''}#${(s.text || '').length}`)),
      });
    }
    const art = document.querySelector(`[data-turn-id="${turnId}"]`);
    const nodes = art
      ? [...art.querySelectorAll('.md-content')]
        .filter((n) => n.textContent.includes(needle))
      : [];
    const blocks = art ? art.querySelectorAll('.conversation-block').length : 0;
    maxN = Math.max(maxN, nodes.length);
    if (nodes.length > 0 || (ev.projectionRevision >= 222
        && ev.projectionRevision <= 226)) {
      report.push({
        rev: ev.projectionRevision, seq: ev.sequence, type: ev.type,
        n: nodes.length, blocks,
        sig: nodes.map((n) => `${describe(n).kind}#${describe(n).bid}`),
      });
    }
    if (nodes.length > 1) {
      dupAt = { rev: ev.projectionRevision, seq: ev.sequence,
                nodes: nodes.map(describe) };
      break;
    }
  }
  return { report, dupAt, maxN, snapCount, translateCount };
}
"""


def test_turn16_real_stream_replay(page):
    from tests.conftest import _dismiss_onboarding_modals

    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    time.sleep(0.4)
    _dismiss_onboarding_modals(page)

    page.locator('#userInput').fill('host message for replay')
    page.locator('#sendBtn').click()
    page.wait_for_function(
        f"document.querySelector('#chatInner')"
        f"   && document.querySelector('#chatInner').innerText.includes"
        f"('{_FINAL_TEXT}')",
        timeout=30000)
    conv_id = page.evaluate(
        "() => (typeof activeConvId !== 'undefined' && activeConvId) || "
        "       (document.querySelector('.conv-item')?.dataset.convId || '')")
    assert conv_id, 'no active conversation'

    with open('debug/_scratch/turn16_events.json') as fh:
        events = json.load(fh)
    with open('debug/_scratch/turn16_seed_rev73.json') as fh:
        seed_projection = json.load(fh)

    result = page.evaluate(
        _REPLAY_JS,
        {
            'events': events,
            'seedProjection': seed_projection,
            'turnId': '770f2865-b818-4bfd-94e5-74d87017ef51',
            'needle': '我先检查后台导出任务的状态',
            'convId': conv_id,
        })

    out = os.path.join(os.path.dirname(__file__), '..', 'screenshots',
                       'turn16_replay_diag.json')
    with open(out, 'w') as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    print(f'\nreplay report written: {out}')
    print('\n===== REPLAY TIMELINE (revs with needle nodes) =====')
    print(f"snapshots applied: {result.get('snapCount')}, "
          f"translate frames: {result.get('translateCount')}")
    for row in result['report']:
        if row.get('inv'):
            print(f"rev {row['rev']} INV contentLen={row['contentLen']} segs={row['segs']}")
        else:
            print(f"rev {row['rev']} seq {row['seq']} {row['type']:<22} "
                  f"needle={row['n']} blocks={row['blocks']}  {row['sig']}")
    print(f"\nmax simultaneous needle nodes: {result['maxN']}")
    if result['dupAt']:
        print('\n===== DUPLICATE DETECTED =====')
        print(json.dumps(result['dupAt'], ensure_ascii=False, indent=1))
    else:
        print('\nno duplicate needle nodes at any replayed revision')
