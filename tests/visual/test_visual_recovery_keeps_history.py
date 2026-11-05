"""A recovery snapshot never unmounts history the reader already sees.

The production Turn runtime, sync coordinator, TurnStore, view-model selector
and Surface run in Chromium; only transport is faked in-page. While a long Turn
streams, sync.reset_required forces a recovery snapshot whose byte-budgeted
tail window admits only that Turn. Every older Turn node must stay mounted at
every commit and frame; the runtime then re-reads the range and removes only
the Turn the server deleted meanwhile.
"""
import json
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

ROOT = Path(__file__).resolve().parents[2]


def test_tail_window_recovery_keeps_rendered_history_mounted(frontend_visual_page):
    page = frontend_visual_page
    for name, source in [
        ('runtime', 'frontend/src/core/turn-runtime.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('session', 'frontend/src/conversation/application/conversation-session.ts'),
        ('renderers', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'recovery-history-{name}.js', source))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    result = page.evaluate(r"""async locale => {
      document.body.innerHTML = '<main id="surface"></main>';
      const turn = (turnId, ordinal, status = 'completed') => ({
        turnId, presentationId: turnId, conversationId: 'conv', laneId: 'main', ordinal,
        actor: 'assistant', kind: 'reply', runId: '', status, currentAttemptId: null,
        projectionRevision: 1, createdAt: ordinal, updatedAt: ordinal, settlement: {},
        projection: {content: `Answer ${turnId}`, segments: [{type: 'text', blockId: `${turnId}-text`,
          text: `Answer ${turnId}`, terminal: true, deliverable: true}]},
      });
      const snapshot = (syncSeq, turns, totalTurns) => ({
        ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
        scope: {kind: 'conversation', ownerId: 7, threadId: 'conv'}, conversationId: 'conv',
        conversationRevision: syncSeq, syncSeq, cursor: `cursor-${syncSeq}`, serverBootId: 'test',
        heartbeatIntervalMs: 15000, settings: {}, queueItems: [], attempts: [], turns,
        turnWindow: {laneId: 'main', nextBeforeOrdinal: turns[0].ordinal, hasMore: true, totalTurns},
      });
      const olderPage = (syncSeq, beforeOrdinal, turns, totalTurns) => ({
        ok: true, contract: 'tofu.conversation-sync.turn-page/v1', conversationId: 'conv',
        conversationRevision: syncSeq, syncSeq, cursor: `cursor-${syncSeq}`, laneId: 'main',
        beforeOrdinal, nextBeforeOrdinal: turns[0].ordinal, hasMore: false, totalTurns, turns,
        attempts: [],
      });
      const live = turn('live', 5, 'running');
      const snapshots = [snapshot(1, [turn('input', 3), turn('answer', 4), live], 6),
        snapshot(2, [live], 5)];
      const pages = [];
      const sources = [];
      const api = {
        async snapshot() { return snapshots.shift(); },
        async turnPage(_id, _lane, syncSeq, beforeOrdinal) {
          pages.push(beforeOrdinal);
          return beforeOrdinal === 3
            ? olderPage(syncSeq, 3, [turn('first', 0), turn('deleted', 1), turn('third', 2)], 6)
            : olderPage(syncSeq, 5, [turn('first', 0), turn('third', 2), turn('input', 3),
              turn('answer', 4)], 5);
        },
      };
      class FakeSource extends EventTarget {
        constructor() {
          super();
          Object.assign(this, {readyState: 1, onopen: null, onerror: null, onmessage: null});
          sources.push(this);
        }
        close() { this.readyState = 2; }
      }
      const runtime = createConversationTurnRuntime({api, isActive: () => true,
        renderState: () => true, eventSourceFactory: () => new FakeSource()});
      const conversation = {id: 'conv'};
      const store = await runtime.hydrateConversation(conversation);
      const surface = createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({renderSafeMarkdownHtml: value => value,
          localizedText: (key, fallback) => locale[key] || fallback}));
      const session = bindConversationSession(store, surface);
      await runtime.loadConversationTurnPage(conversation, 'main', 3, 64);

      const mounted = () => [...document.querySelectorAll('#surface [data-turn-id]')]
        .map(node => node.dataset.turnId);
      const nodeOf = id => document.querySelector(`#surface [data-turn-id="${id}"]`);
      const before = mounted();
      const history = ['first', 'third', 'input', 'answer'];
      const nodes = new Map(history.map(id => [id, nodeOf(id)]));
      const missingAtCommit = [], missingInFrame = [];
      const missing = () => history.filter(id => !nodes.get(id)?.isConnected);
      // The session renders synchronously inside its own subscription, which
      // was registered first; this listener therefore sees each committed DOM.
      store.subscribe(() => { if (missing().length) missingAtCommit.push(missing()); });
      let sampling = true;
      const sample = () => {
        if (!sampling) return;
        if (missing().length) missingInFrame.push(missing());
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);

      sources.at(-1).onmessage(new MessageEvent('message', {data: JSON.stringify({
        contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv',
        type: 'sync.reset_required', cursor: 'cursor-2', reason: 'cursor_invalid'})}));
      const deadline = performance.now() + 5000;
      while (performance.now() < deadline
          && (pages.length < 2 || store.getState().turnsById.deleted)) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      sampling = false;
      const result = {before, after: mounted(), pages, missingAtCommit, missingInFrame,
        sameNodes: history.every(id => nodeOf(id) === nodes.get(id)),
        retained: store.getState().historyByLane.main?.retainedFromOrdinal ?? null};
      session.dispose();
      runtime.disposeConversation('conv');
      return result;
    }""", locale)

    assert result['before'] == ['first', 'deleted', 'third', 'input', 'answer', 'live']
    assert result['missingAtCommit'] == []
    assert result['missingInFrame'] == []
    assert result['sameNodes'] is True
    assert result['pages'] == [3, 5]
    assert result['after'] == ['first', 'third', 'input', 'answer', 'live']
    assert result['retained'] is None
