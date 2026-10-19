"""Startup phase follows committed event evidence through the real UI owners."""
import json

import pytest
from playwright.sync_api import expect

from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_workspace_phase_and_equal_revision_clear_reach_dom(page, language):
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    for name, source in [
        ('state', 'frontend/src/core/turn-state.ts'),

        ('viewmodel', 'frontend/src/conversation/presentation/conversation-view-model.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('session', 'frontend/src/conversation/application/conversation-session.ts'),
        ('renderers', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'startup-{name}.js', source))
    page.evaluate("""locale => {
      const host = document.createElement('main'); host.id = 'startup-surface';
      document.body.replaceChildren(host);
      const renderers = createClassicConversationRenderers({
        renderSafeMarkdownHtml: value => value,
        localizedText: (key, fallback) => locale[key] || fallback,
      });
      const surface = createConversationSurface(host, renderers);
      const store = createTurnStore('startup');

      window.startupStore = store;
      const session = bindConversationSession(store, surface);
      const turn = revision => ({turnId:'answer',presentationId:'answer',conversationId:'startup',
        laneId:'main',ordinal:1,actor:'assistant',kind:'reply',runId:'',status:'running',
        currentAttemptId:'attempt',projectionRevision:revision,createdAt:1,updatedAt:revision,
        settlement:{},projection:{content:''}});
      store.dispatch({type:'snapshot',snapshot:{authoritativeFull:true,conversationRevision:1,
        turns:[turn(1)],attempts:[{attemptId:'attempt',turnId:'answer',status:'running',lastSeq:1}],
        activeAttemptPhase:{turnId:'answer',attemptId:'attempt',projectionRevision:1,
          phase:{phase:'working',detailKey:'stream.phase.workspacePreparing'}}}});
      window.clearStartupAfterAck = () => {
        store.dispatch({type:'command_response',response:{conversationRevision:2,turn:turn(2)}});
        store.dispatch({type:'event',event:{conversationId:'startup',turnId:'answer',
          attemptId:'attempt',seq:2,projectionRevision:2,type:'projection_updated',
          payload:{projection:{content:''},phase:null}}});
      };
      window.disposeStartup = () => session.dispose();
    }""", locale)
    node = page.locator('#startup-surface')

    assert page.evaluate('startupStore.getState().livePhase') == {
        'phase': 'working', 'detailKey': 'stream.phase.workspacePreparing'}

    assert page.evaluate("selectConversationViewModel(startupStore.getState()).mainLane.turns[0].blocks.map(b=>b.kind)") == ['live-status']
    try:
        expect(node).to_contain_text(locale['stream.phase.workspacePreparing'])
        page.evaluate('clearStartupAfterAck()')
        expect(node).not_to_contain_text(locale['stream.phase.workspacePreparing'])
        expect(node).not_to_contain_text(locale['stream.phase.workerStarting'])
    finally:
        page.evaluate('disposeStartup()')
