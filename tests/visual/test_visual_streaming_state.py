"""Immutable live-event reduction through the real browser session and Surface.

Uses a local HTTP fixture and the actual TypeScript graph. Synthetic state never
enters application storage. Checks reader state and terminal/recovery semantics,
not host-dependent wall-clock thresholds.
"""
import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize("binding", ["session", "controller"])
def test_streaming_preserves_reader_state_and_recovery_uses_new_authority(frontend_visual_page, binding):
    page = frontend_visual_page
    for name, source in [
        ('state', 'frontend/src/core/turn-state.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('session', 'frontend/src/conversation/application/conversation-session.ts'),
        ('controller', 'frontend/src/conversation/application/conversation-surface-controller.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/state-sharing-{name}.js', ROOT / source))
    page.evaluate("""binding => {
      document.body.innerHTML='<main id="surface"></main>';
      window.store=createTurnStore('stream-sharing');
      const turns=Array.from({length: 800},(_,index)=>({
        turnId:'turn-'+index,presentationId:'turn-'+index,conversationId:'stream-sharing',
        laneId:'main',ordinal:index+1,actor:'assistant',kind:'reply',runId:'',
        status:index===799?'running':'completed',currentAttemptId:'attempt-'+index,
        projectionRevision:1,projection:{content:'Evidence '+index,segments:[{
          type:'text',blockId:'answer',text:'Evidence '+index,terminal:index!==799,deliverable:index!==799
        }]},settlement:{},createdAt:1,updatedAt:1
      }));
      store.dispatch({type:'snapshot',snapshot:{conversationRevision:1,turns,
        attempts:[{attemptId:'attempt-799',turnId:'turn-799',lastSeq:1,status:'running'}]}});
      window.original=store.getState();
      const scheduler={schedule(render){
        const id=requestAnimationFrame(render);return ()=>cancelAnimationFrame(id);
      }};
      if(binding==='session') {
        window.surface=createConversationSurface(document.getElementById('surface'));
        window.session=bindConversationSession(store,surface,{scheduler});
      } else {
        const controller=createConversationSurfaceController({
          isActive:id=>id==='stream-sharing',getContainer:()=>document.getElementById('surface'),
          schedule:scheduler.schedule
        });
        const conv={id:'stream-sharing'};
        const unsubscribe=store.subscribe((state,update)=>controller.render(conv,state,{update}));
        controller.render(conv,store.getState());
        window.session={dispose(){unsubscribe();controller.dispose();}};
      }
    }""", binding)
    page.wait_for_function("document.querySelector('[data-turn-id=\"turn-798\"]')")
    page.evaluate("""() => {
      window.readerTurn=document.querySelector('[data-turn-id="turn-798"]');
      const input=document.createElement('input');input.id='reader-draft';input.value='My correction';
      readerTurn.append(input);input.focus();input.setSelectionRange(3,7);
      window.deliver=(seq,terminal=false)=>store.dispatch({type:'event',event:{
        turnId:'turn-799',attemptId:'attempt-799',seq,projectionRevision:seq,
        type:terminal?'terminal_settlement':'projection_updated',payload:{
          projection:{content:'Live result '+seq,segments:[{
            type:'text',blockId:'answer',text:'Live result '+seq,terminal,deliverable:terminal
          }]},
          ...(terminal?{status:'completed',settlement:{outcome:'success'}}:{})
        }
      }});
      // All these authoritative frames coalesce into the next browser paint.
      for(let seq=2;seq<=41;seq++)deliver(seq);
    }""")
    try:
        page.wait_for_function("document.querySelector('[data-turn-id=\"turn-799\"]')?.textContent.includes('Live result 41')", timeout=5000)
    except Exception as error:
        details = page.evaluate("""() => ({state:store.getState().turnsById['turn-799'],
          text:document.getElementById('surface').textContent.slice(-1200),
          nodes:[...document.querySelectorAll('[data-turn-id]')].map(n=>n.dataset.turnId).slice(-4)})""")
        raise AssertionError(details) from error
    assert page.evaluate("""() => {
      const state=store.getState(),input=document.getElementById('reader-draft');
      return document.querySelector('[data-turn-id="turn-798"]')===readerTurn
        && document.activeElement===input && input.value==='My correction'
        && input.selectionStart===3 && input.selectionEnd===7
        && state.laneOrder===original.laneOrder && state.queueItems===original.queueItems
        && original.turnsById['turn-799'].projection.content==='Evidence 799';
    }""")
    page.evaluate('deliver(42,true)')
    page.wait_for_function("document.querySelector('[data-turn-id=\"turn-799\"]')?.textContent.includes('Live result 42')")
    assert page.evaluate("store.getState().turnsById['turn-799'].status") == 'completed'
    assert page.evaluate("""() => {
      const settled=store.getState();deliver(41);return settled===store.getState();
    }""")
    page.evaluate("""() => store.dispatch({type:'snapshot',snapshot:{
      authoritativeFull:true,conversationRevision:2,turns:[{
        ...original.turnsById['turn-798'],projectionRevision:2,projection:{content:'Recovered authority',segments:[{
          type:'text',blockId:'answer',text:'Recovered authority',terminal:true,deliverable:true
        }]}
      }],attempts:[]
    }})""")
    page.wait_for_function("document.getElementById('surface').textContent.includes('Recovered authority')")
    assert page.locator('[data-turn-id="turn-799"]').count() == 0
    page.evaluate('session.dispose()')
    assert page.locator('#surface .conversation-surface').count() == 0
