"""Real browser replay must not amplify disposable automation reads."""
import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def test_forced_conversation_paints_do_not_repeat_time_reads(frontend_visual_page):
    page = frontend_visual_page
    page.set_content('<main id="surface"></main><button id="responsive">Responsive</button>')
    for name, source in [
        ('controller', 'frontend/src/conversation/application/conversation-surface-controller.ts'),
        ('state', 'frontend/src/core/turn-state.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/task-time-{name}.js', ROOT / source))
    result = page.evaluate("""async () => {
      let reads=0, scheduled;
      const controller=createConversationSurfaceController({
        isActive:()=>true,getContainer:()=>document.getElementById('surface'),
        schedule:render=>{scheduled=render;return ()=>{scheduled=null}},
        loadTaskTimePresentations:async()=>{reads++;return {items:[],nextCursor:''}}
      });
      const state=createTurnState('conv');
      state.turnsById.turn={turnId:'turn',presentationId:'turn',conversationId:'conv',
        laneId:'main',ordinal:1,actor:'assistant',kind:'message',runId:'',status:'completed',
        currentAttemptId:'attempt',projectionRevision:1,settlement:{},createdAt:1,updatedAt:1,
        projection:{segments:[{type:'text',blockId:'text',text:'Answer'}]}};
      state.laneOrder.main=['turn'];
      state.attemptsById.attempt={attemptId:'attempt',startedAt:1};
      const settle=()=>new Promise(resolve=>setTimeout(resolve,0));
      try {
        for(let i=0;i<40;i++){
          controller.render({id:'conv'},state,{force:true});scheduled();await settle();
        }
        const initialReads=reads;
        controller.invalidateTaskTimePresentations('conv');await settle();
        const refreshReads=reads;
        controller.dispose();await settle();
        return {initialReads,refreshReads};
      } finally {controller.dispose()}
    }""")
    assert result == {'initialReads': 1, 'refreshReads': 2}
    page.locator('#responsive').click(timeout=2000)
