"""Committed answers converge through the real runtime/controller/Surface graph.

The local HTTP origin and native EventSource exercise browser delivery. Only
server responses and presentation failures are controlled; there is no live
model call, user conversation mutation or alternate DOM renderer.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_graph
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual


def _snapshot(head: int) -> dict:
    status = 'running' if head == 1 else 'completed'
    answer = '' if head == 1 else 'Complete committed answer, without refreshing.'
    return {
        'ok': True, 'contract': 'tofu.conversation-sync.snapshot/v1',
        'scope': {'kind': 'conversation', 'ownerId': 7, 'threadId': 'convergence'},
        'conversationId': 'convergence', 'conversationRevision': head,
        'syncSeq': head, 'cursor': f'cursor-{head}', 'serverBootId': 'test',
        'heartbeatIntervalMs': 15000, 'settings': {}, 'queueItems': [],
        'turns': [{
            'turnId': 'answer', 'presentationId': 'answer',
            'conversationId': 'convergence', 'laneId': 'main', 'ordinal': 1,
            'actor': 'assistant', 'kind': 'reply', 'runId': '', 'status': status,
            'currentAttemptId': 'attempt', 'projectionRevision': 1 if head == 1 else 2,
            'createdAt': 1, 'updatedAt': head, 'settlement': {},
            'projection': {'content': answer},
        }],
        'attempts': [{'attemptId': 'attempt', 'turnId': 'answer',
                      'conversationId': 'convergence', 'status': status,
                      'commandId': 'command', 'taskId': 'task', 'operation': 'generate',
                      'baseProjectionRevision': 1, 'resumeAnchor': {}, 'createdAt': 1}],
    }


@pytest.mark.parametrize('fault', [
    'snapshot-race', 'presentation-failure', 'renderer-failure',
    'receipt-failure', 'withheld-frame', 'cancellation-failure',
])
def test_terminal_answer_converges_without_reload(frontend_visual_page, tmp_path, fault):
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_viewport_size({'width': 1180, 'height': 900})
    page.add_script_tag(path=native_module_graph([
        ('.native/terminal-convergence-runtime.js', 'frontend/src/core/turn-runtime.ts'),
        ('.native/terminal-convergence-controller.js', 'frontend/src/conversation/application/conversation-surface-controller.ts'),
        ('.native/terminal-convergence-renderers.js', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
        ('.native/terminal-convergence-scheduler.js', 'frontend/src/conversation/ui/animation-frame-scheduler.ts'),
    ]))
    heads = []
    cursors = []

    def serve_snapshot(route):
        head = 1 if not heads else (2 if len(heads) == 1 else 3)
        heads.append(head)
        if fault == 'snapshot-race' and head == 2:
            # The HTTP snapshot was captured before a newer, partial command
            # ACK. That ACK does not contain the answer's terminal revision.
            page.evaluate("""() => convergenceStore.dispatch({type:'command_response', response:{
              conversationRevision:3,
              turn:{turnId:'unrelated',presentationId:'unrelated',conversationId:'convergence',
                laneId:'main',ordinal:2,actor:'human',kind:'input',runId:'',status:'completed',
                currentAttemptId:null,projectionRevision:1,createdAt:1,updatedAt:3,
                settlement:{},projection:{content:'A newer partial command acknowledgement'}}
            }})""")
        route.fulfill(content_type='application/json', body=json.dumps(_snapshot(head)))

    def serve_events(route):
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(route.request.url).query)
        assert query['streamClientId'] == ['convergence-page']
        [stream] = json.loads(query['streams'][0])
        cursor = stream['after']
        cursors.append(cursor)
        event = {'contract': 'tofu.conversation-sync.event/v1',
                 'conversationId': 'convergence'}
        if cursor == 'cursor-1':
            event.update(type='sync.reset_required', cursor='reset-2', reason='server_restarted')
        else:
            event.update(type='sync.heartbeat', cursor=cursor, serverBootId='test', degraded=False)
        route.fulfill(content_type='text/event-stream',
                      body=f"event: {event['type']}\ndata: 0\ndata: {json.dumps(event)}\n\n")

    page.route('**/convergence-fixture/snapshot', serve_snapshot)
    page.route('**/convergence-fixture/events?*', serve_events)
    page.evaluate("""fault => {
      document.body.innerHTML='<main id="surface"></main>';
      const conversation={id:'convergence'};
      let failed=false;
      const commits=[];
      const renderClock=fault==='withheld-frame' || fault==='cancellation-failure' ? {
        requestAnimationFrame:callback=>fault==='withheld-frame' ? 1 : window.requestAnimationFrame(callback),
        cancelAnimationFrame:handle=>window.cancelAnimationFrame(handle),
        setTimeout:(callback,delay)=>window.setTimeout(callback,delay),
        clearTimeout:handle=>{
          if(fault==='cancellation-failure') throw new Error('Controlled timer cancellation failure');
          window.clearTimeout(handle);
        },
      } : window;
      const scheduler=createAnimationFrameScheduler(renderClock);
      const nativeRenderers=createClassicConversationRenderers({renderSafeMarkdownHtml:value=>value});
      const renderBlock=nativeRenderers.renderBlock;
      nativeRenderers.renderBlock=(node,block,context)=>{
        if(fault==='renderer-failure' && !failed && context.turn.status==='completed') {
          failed=true; throw new Error('Controlled terminal block renderer failure');
        }
        renderBlock(node,block,context);
      };
      const controller=createConversationSurfaceController({
        isActive:id=>id===conversation.id,
        getContainer:()=>document.getElementById('surface'),
        schedule:render=>scheduler.schedule(render),
        nativeRenderers,
        costSignatureSnapshot:state=>{
          if(fault==='presentation-failure' && !failed && state.turnsById.answer?.status==='completed') {
            failed=true; throw new Error('Controlled terminal presentation failure');
          }
          return new Map();
        },
        afterConversationCommit:(_conversation,state)=>{
          if(fault==='receipt-failure' && !failed && state.turnsById.answer?.status==='completed') {
            failed=true; throw new Error('Controlled terminal commit receipt failure');
          }
          commits.push({revision:state.conversationRevision,status:state.turnsById.answer?.status,
            answer:document.getElementById('surface').textContent});
        },
      });
      const failures=[];
      const runtime=createConversationTurnRuntime({
        api:{
          async snapshot(_id,options){
            const response=await fetch('/convergence-fixture/snapshot',{signal:options.signal});
            return response.json();
          },
          multiplexedEventsUrl:(client,streams)=>'/convergence-fixture/events?streamClientId='
            +encodeURIComponent(client)+'&streams='+encodeURIComponent(JSON.stringify(streams)),
        },
        streamClientId:'convergence-page',
        isActive:()=>true,
        renderState:(conversation,state,context)=>controller.render(conversation,state,context),
        onProtocolError:error=>failures.push(error.message),
      });
      window.convergenceRuntime=runtime;
      window.convergenceCommits=commits;
      window.convergenceFailures=failures;
      window.disposeConvergence=()=>{runtime.disposeConversation(conversation.id);controller.dispose();};
      void runtime.hydrateConversation(conversation).then(store=>{window.convergenceStore=store;});
    }""", fault)
    try:
        expect(page.locator('#surface')).to_contain_text(
            'Complete committed answer, without refreshing.', timeout=10000)
        expect(page.locator('#surface')).not_to_contain_text('Waiting for an execution status update')
        assert page.evaluate('convergenceRuntime.readRuntimeState("convergence").turnsById.answer.status') == 'completed'
        assert page.locator('[data-turn-id="answer"]').count() == 1
        assert page.locator('[data-turn-id="answer"]').get_attribute('data-turn-status') == 'completed'
        if fault == 'snapshot-race':
            assert heads == [1, 2, 3]
            assert cursors[:2] == ['cursor-1', 'cursor-3']
            failures = page.evaluate('convergenceFailures')
            assert failures
            assert set(failures) == {'Conversation snapshot is stale and was not applied'}
        else:
            assert heads == [1, 2]
            assert page.evaluate('convergenceFailures') == []
        # Text can mount before a required receipt fails. Wait for the bounded
        # repair to acknowledge the whole commit, not merely its earlier DOM write.
        page.wait_for_function('convergenceCommits.some(commit=>commit.status==="completed")')
        page.screenshot(path=str(tmp_path / f'terminal-convergence-{fault}.png'))
    finally:
        page.evaluate('disposeConvergence()')


@pytest.mark.parametrize('has_artifacts', [False, True])
def test_artifact_mount_demand_is_not_a_canonical_commit_ack(frontend_visual_page, has_artifacts):
    from pathlib import Path
    from playwright.sync_api import expect
    from tests._paper_vite import compiled_typescript

    page = frontend_visual_page
    page.add_script_tag(path=native_module_graph([
        ('.native/artifact-commit-controller.js', 'frontend/src/conversation/application/conversation-surface-controller.ts'),
        ('.native/artifact-commit-state.js', 'frontend/src/core/turn-state.ts'),
        ('.native/artifact-commit-scheduler.js', 'frontend/src/conversation/ui/animation-frame-scheduler.ts'),
        ('.native/artifact-commit-renderers.js', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]))
    page.evaluate('window.runtimeScope=window')
    root = Path(__file__).resolve().parents[2]
    source = root / 'frontend/src/runtime/sections/artifacts.js'
    body = 'const runtimeScope=window;\n' + source.read_text()
    for owner in ('artifact-delivery', 'artifact-review'):
        body = body.replace(f"'../rich-response/{owner}'",
                            repr(str(root / f'frontend/src/rich-response/{owner}.ts')))
    with compiled_typescript(source, contents=body) as bundle:
        page.add_script_tag(path=bundle)

    page.evaluate("""hasArtifacts => {
      document.body.innerHTML='<main id="artifact-surface"></main>';
      const conversation={id:'artifact-commit'};
      const store=createTurnStore(conversation.id);
      store.dispatch({type:'snapshot',snapshot:{conversationRevision:1,turns:[{
        turnId:'answer',presentationId:'answer',conversationId:conversation.id,laneId:'main',
        ordinal:1,actor:'assistant',kind:'reply',runId:'',status:'completed',currentAttemptId:null,
        projectionRevision:1,createdAt:1,updatedAt:1,settlement:{},projection:{content:'Complete answer'},
      }]}});
      const scheduler=createAnimationFrameScheduler(window);
      const evidence={reads:0,mounts:0,receipts:0,mountStates:[]};
      window.Api={artifacts:{forConv:async()=>{evidence.reads++;
        return {artifacts:[{id:'output',turn_id:'answer',format:'file',title:'Published output'}]};}}};
      window.ConversationTurnRead={state:()=>store.getState()};
      const controller=createConversationSurfaceController({
        isActive:id=>id===conversation.id,getContainer:()=>document.getElementById('artifact-surface'),
        schedule:render=>scheduler.schedule(render),
        nativeRenderers:createClassicConversationRenderers({renderSafeMarkdownHtml:value=>value}),
        onConversationMounted:()=>{
          evidence.mounts++;
          evidence.mountStates.push({mounted:controller.isMounted(conversation.id),
            acknowledged:controller.ownsConversation(conversation.id)});
          Artifacts.activateConversation(conversation);
        },
        afterConversationCommit:()=>{evidence.receipts++;},
      });
      window.ConversationSurfacePresentation={
        isMounted:id=>controller.isMounted(id),ownsConversation:id=>controller.ownsConversation(id),
        setArtifacts:(id,byTurn)=>controller.setArtifacts(id,byTurn),
      };
      window.artifactCommitEvidence=evidence;
      window.artifactCommitController=controller;
      // Snapshot hints may precede a mounted body; mount demand consumes them.
      void Artifacts.hydrateConversation(conversation,hasArtifacts).then(()=>{
        controller.render(conversation,store.getState());
      });
    }""", has_artifacts)
    try:
        expect(page.locator('#artifact-surface')).to_contain_text('Complete answer')
        page.wait_for_function('artifactCommitController.ownsConversation("artifact-commit")')
        if has_artifacts:
            expect(page.locator('#artifact-surface')).to_contain_text('Published output')
        else:
            expect(page.locator('#artifact-surface')).not_to_contain_text('Published output')
        evidence = page.evaluate('artifactCommitEvidence')
        assert evidence['reads'] == (1 if has_artifacts else 0)
        assert evidence['mounts'] == 1
        assert evidence['receipts'] == 2
        assert evidence['mountStates'] == [{'mounted': True, 'acknowledged': False}]
    finally:
        page.evaluate('artifactCommitController.dispose()')
