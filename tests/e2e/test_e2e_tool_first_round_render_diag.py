"""DIAGNOSTIC (throwaway): first tool round inside turn blocks renders twice.

Boots the real app + real Chromium + stub LLM (same seams as
tests/e2e/test_e2e_smoke.py), sends a message that drives TWO web_search tool
rounds followed by closing text, and records the live DOM evolution of the
assistant turn's blocks:

- coarse block signature sampled on change (kind, blockId, row count, text),
- every add/remove mutation touching .conversation-block / .ptool-panel nodes.

The printed timeline answers: is the first round's content mounted twice
(duplicate nodes), remounted (remove+add), or rebuilt in place (inner panel
swapped)?
"""
from __future__ import annotations

import json
import time

import pytest

pytestmark = [pytest.mark.visual]

_SENTINEL = {'stream_calls': 0, 'search_calls': 0}

_FINAL_TEXT = 'diag final answer marker'


def _stub_stream_llm_response_factory():
    def _stub(task, body, tag='', on_tool_call_ready=None):
        _SENTINEL['stream_calls'] += 1
        round_no = int(task.get('_e2e_diag_round') or 0)
        if round_no < 2:
            task['_e2e_diag_round'] = round_no + 1
            # Simulate model thinking time so the live "running" panel state
            # persists long enough to observe.
            time.sleep(0.7)
            # Round 1: TWO parallel calls (fan-out), matching real search
            # batches. Round 2: narration prose + one follow-up call.
            specs = ([('call_e2e_0a', 'e2e query alpha'),
                      ('call_e2e_0b', 'e2e query beta')]
                     if round_no == 0
                     else [('call_e2e_1', 'e2e query gamma')])
            calls = []
            for idx, (call_id, query) in enumerate(specs):
                tc = {
                    'id': call_id, 'index': idx, 'type': 'function',
                    'function': {'name': 'web_search',
                                 'arguments': json.dumps({'query': query})},
                }
                calls.append(tc)
                if on_tool_call_ready:
                    try:
                        on_tool_call_ready(tc)
                    except Exception:
                        pass
            narration = ('' if round_no == 0
                         else 'narration between rounds. ')
            if narration:
                with task['content_lock']:
                    task['content'] += narration
                from lib.agent_core.events import EventType, build_event
                from lib.tasks_pkg.manager._events import append_event
                append_event(task, build_event(
                    EventType.DELTA, content=narration))
            return ({'role': 'assistant',
                     'content': narration,
                     'thinking': f'round {round_no} reasoning trace. ',
                     'tool_calls': calls},
                    'tool_calls',
                    {'prompt_tokens': 10, 'completion_tokens': 2,
                     'total_tokens': 12})

        words = _FINAL_TEXT.split()
        for i, w in enumerate(words):
            if task.get('aborted'):
                break
            cd = w + (' ' if i < len(words) - 1 else '')
            with task['content_lock']:
                task['content'] += cd
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager._events import append_event
            append_event(task, build_event(EventType.DELTA, content=cd))
            time.sleep(0.03)
        return ({'role': 'assistant', 'content': _FINAL_TEXT, 'tool_calls': []},
                'stop',
                {'prompt_tokens': 20, 'completion_tokens': len(words),
                 'total_tokens': 20 + len(words)})

    return _stub


def _stub_perform_web_search(query, user_question='', freshness='', **kwargs):
    _SENTINEL['search_calls'] += 1
    time.sleep(0.5)  # simulate execution so the running panel is observable
    return [{
        'title': f'E2E stub result for {query}',
        'snippet': 'A deterministic search result for the diagnostic test.',
        'url': 'https://example.invalid/e2e',
        'source': 'stub',
    }]


@pytest.fixture(scope='module', autouse=True)
def _install_llm_stubs():
    import tofu_search
    import lib.tasks_pkg.llm_fallback._call as llm_fb
    import lib.tasks_pkg.handlers.search._core as search_core

    saved = {
        'llm_fb': llm_fb.stream_llm_response,
        'search_core': search_core.perform_web_search,
        'tofu': getattr(tofu_search, 'perform_web_search', None),
    }
    llm_fb.stream_llm_response = _stub_stream_llm_response_factory()
    tofu_search.perform_web_search = _stub_perform_web_search
    search_core.perform_web_search = _stub_perform_web_search
    try:
        yield
    finally:
        llm_fb.stream_llm_response = saved['llm_fb']
        search_core.perform_web_search = saved['search_core']
        if saved['tofu'] is not None:
            tofu_search.perform_web_search = saved['tofu']


_INSTRUMENT_JS = r"""
() => {
  window.__diag = { t0: performance.now(), samples: [], mutations: [],
                    renders: [] };
  const norm = (s) => (s || '').replace(/\s+/g, ' ');
  const hash = (s) => {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return h.toString(36);
  };
  const blockHtml = {};
  const trackRenders = () => {
    for (const b of document.querySelectorAll('.conversation-block--tool')) {
      const id = b.dataset.blockId || '?';
      const h = hash(b.innerHTML);
      if (blockHtml[id] !== h) {
        blockHtml[id] = h;
        window.__diag.renders.push({
          t: Math.round(performance.now()), id, hash: h,
          len: norm(b.textContent).length,
          text: norm(b.textContent).slice(0, 90),
          dup: document.querySelectorAll(
            `[data-block-id="${id}"]`).length,
        });
      }
    }
  };
  const docSig = () => {
    const out = {
      surfaces: document.querySelectorAll('[data-conversation-surface]').length,
      turns: document.querySelectorAll('.conversation-turn').length,
      blocks: {},
    };
    for (const b of document.querySelectorAll('.conversation-block')) {
      const id = b.dataset.blockId || '?';
      out.blocks[id] = (out.blocks[id] || 0) + 1;
    }
    return out;
  };
  let lastSig = '';
  window.__diagTimer = setInterval(() => {
    trackRenders();
    const s = docSig();
    const j = JSON.stringify(s);
    if (j !== lastSig) {
      lastSig = j;
      window.__diag.samples.push({ t: Math.round(performance.now()), sig: s });
    }
  }, 40);
  window.__raw = [];
  const desc = (n) => {
    if (n.nodeType !== 1) return n.nodeType === 3 ? `text:${(n.data||'').slice(0,20)}` : `t${n.nodeType}`;
    const el = n;
    const id = el.dataset?.blockId || el.dataset?.presentationId || el.id || '';
    return `${el.tagName.toLowerCase()}.${String(el.className||'').split(' ')[0]}${id ? '#'+id : ''}`;
  };
  const moRaw = new MutationObserver((recs) => {
    for (const r of recs) {
      if (r.type !== 'childList') continue;
      window.__raw.push({
        t: Math.round(performance.now()),
        target: desc(r.target),
        add: [...r.addedNodes].map(desc).slice(0, 8),
        rem: [...r.removedNodes].map(desc).slice(0, 8),
      });
    }
  });
  moRaw.observe(document.documentElement, { subtree: true, childList: true });
  const mo = new MutationObserver((recs) => {
    const note = (n, type) => {
      if (n.nodeType !== 1) return;
      const hits = [];
      if (n.matches?.('.conversation-block,.conversation-turn')) hits.push(n);
      for (const el of n.querySelectorAll?.(
          '.conversation-block,.conversation-turn') || []) hits.push(el);
      for (const el of hits) {
        const isTurn = el.classList.contains('conversation-turn');
        const id = isTurn
          ? (el.dataset.presentationId || el.dataset.turnId || '')
          : (el.dataset.blockId || '');
        window.__diag.mutations.push({
          t: Math.round(performance.now()),
          type,
          cls: isTurn ? 'turn' : 'block',
          id,
          kind: el.dataset?.blockKind || el.dataset?.turnStatus || '',
          role: el.dataset?.turnRole || '',
          connected: el.isConnected,
          sameIdInDoc: id
            ? document.querySelectorAll(
                `[data-block-id="${id}"], [data-presentation-id="${id}"]`
              ).length
            : 0,
          snippet: norm(el.textContent).slice(0, 50),
        });
      }
    };
    for (const r of recs) {
      for (const n of r.addedNodes) note(n, 'add');
      for (const n of r.removedNodes) note(n, 'remove');
    }
  });
  mo.observe(document.documentElement, { subtree: true, childList: true });
}
"""


def _wait_app_ready(page, timeout=15000):
    page.wait_for_selector('#userInput', state='visible', timeout=timeout)
    page.wait_for_function(
        "window.TofuModules && window.TofuModules.version === 3",
        timeout=timeout)


def test_first_tool_round_render_timeline(page):
    import os

    from tests.conftest import _dismiss_onboarding_modals

    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    time.sleep(0.4)
    _dismiss_onboarding_modals(page)

    page.evaluate(_INSTRUMENT_JS)

    page.locator('#userInput').fill('__e2e_tool__ run two searches')
    page.locator('#sendBtn').click()

    page.wait_for_function(
        f"document.querySelector('#chatInner').innerText.includes("
        f"'{_FINAL_TEXT}')",
        timeout=60000)
    time.sleep(0.6)  # let the settle pass land

    diag = page.evaluate(
        "() => { clearInterval(window.__diagTimer);\n"
        "  window.__diag.raw = window.__raw; return window.__diag; }")

    # Phase 2: full page reload -> the conversation reopens from the
    # authoritative snapshot (referenced tool segments materialized
    # client-side). Keep tracking the first round's render hashes.
    page.reload()
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    page.evaluate(_INSTRUMENT_JS)
    # Reopen the conversation from the sidebar if it is not active.
    item = page.locator('.conv-item').first
    item.wait_for(state='visible', timeout=10000)
    item.click()
    page.wait_for_function(
        "document.querySelector('#chatInner')"
        "   && document.querySelector('#chatInner').innerText.includes"
        f"('{_FINAL_TEXT}')",
        timeout=30000)
    time.sleep(1.2)
    diag2 = page.evaluate(
        "() => { clearInterval(window.__diagTimer);\n"
        "  window.__diag.raw = window.__raw; return window.__diag; }")
    out2 = os.path.join(os.path.dirname(__file__), '..', 'screenshots',
                        'first_tool_round_diag_reopen.json')
    with open(out2, 'w') as fh:
        json.dump(diag2, fh, ensure_ascii=False)
    print(f'\nreopen diag written: {out2}')
    print('\n===== REOPEN TOOL BLOCK RENDERS =====')
    for r in diag2['renders']:
        print(f"t={r['t']:>6}  id={r['id']!r} hash={r['hash']:>8} "
              f"len={r['len']:>4} dup={r['dup']} {r['text'][:80]!r}")
    counts2 = {}
    for r in diag2['renders']:
        counts2[r['id']] = counts2.get(r['id'], 0) + 1
    print(f'reopen render counts per block: {counts2}')
    print('\n===== REOPEN SAMPLES =====')
    for s in diag2['samples']:
        sig = s['sig']
        blocks = ' | '.join(
            f'{bid}x{n}' if n > 1 else bid
            for bid, n in sorted(sig['blocks'].items()))
        print(f"t={s['t']:>6}  surfaces={sig['surfaces']} "
              f"turns={sig['turns']}  {blocks}")
    out = os.path.join(os.path.dirname(__file__), '..', 'screenshots',
                       'first_tool_round_diag.json')
    with open(out, 'w') as fh:
        json.dump(diag, fh, ensure_ascii=False)
    print(f'\ndiag written: {out}')

    print('\n===== SAMPLES (doc-wide signature on change) =====')
    for s in diag['samples']:
        sig = s['sig']
        blocks = ' | '.join(
            f'{bid}x{n}' if n > 1 else bid
            for bid, n in sorted(sig['blocks'].items()))
        print(f"t={s['t']:>6}  surfaces={sig['surfaces']} "
              f"turns={sig['turns']}  {blocks}")
    print('\n===== TOOL BLOCK RENDERS (innerHTML hash changes) =====')
    for r in diag['renders']:
        print(f"t={r['t']:>6}  id={r['id']!r} hash={r['hash']:>8} "
              f"len={r['len']:>4} dup={r['dup']} {r['text'][:80]!r}")
    counts = {}
    for r in diag['renders']:
        counts[r['id']] = counts.get(r['id'], 0) + 1
    print(f'render counts per block: {counts}')
    print('\n===== MUTATIONS (turn/block add/remove) =====')
    for m in diag['mutations']:
        print(f"t={m['t']:>6}  {m['type']:<6} {m['cls']:<5} "
              f"id={m['id']!r} kind={m['kind']!r} role={m['role']!r} "
              f"conn={m['connected']} sameId={m['sameIdInDoc']} "
              f"{m['snippet'][:46]!r}")
    print('\n===== RAW RECORDS (first 80) =====')
    for r in diag.get('raw', [])[:80]:
        add = ','.join(r['add'])
        rem = ','.join(r['rem'])
        print(f"t={r['t']:>6}  target={r['target']:<42} "
              f"+[{add}] -[{rem}]")
    print('\n===== END DIAG =====')

    assert _SENTINEL['stream_calls'] >= 3, 'stub never drove the tool loop'
    assert _SENTINEL['search_calls'] == 3, 'expected three searches (2+1)'
