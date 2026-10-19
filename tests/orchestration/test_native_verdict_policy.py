"""Native verifier outputs and audit evidence match the retained Python owner."""
import itertools
import sys
import pytest
from lib.identity import PrincipalContext
from lib.agent_verdict import classify_verdict
from tests.orchestration.test_native_flow_engine import native_library as native_library

pytestmark=pytest.mark.unit
TEXTS=['xunresolvedunresolved unresolved','✅unresolved ✅!','approvedapproved approved', None,'','plain feedback','approved','looks good','✅','x✅y','unresolved',
    'xunresolved','unresolved\u0301','unresolved²','unresolved\u200c','xstill NOT met',
    '[VERDICT: STOP]','[VERDICT: STOP] ❌ still failing','[VERDICT: CONTINUE]',
    '[VERDICT: CONTINUE_PLANNER]','[PLAN_DEFECT: wrong topology][VERDICT: CONTINUE_PLANNER]',
    '[PLAN_DEFECT: worker needs more time][VERDICT: CONTINUE_PLANNER]',
    '[PLAN_DEFECT: prior][PLAN_DEFECT: latest][VERDICT: CONTINUE_PLANNER]',
    '[PLAN_DEFECT:    ][VERDICT: CONTINUE_PLANNER]',
    '[VERDICT: STOP][VERDICT: CONTINUE_WORKER]',
    '[VERDICT: CONTINUE_WORKER][VERDICT: STOP]',
    'feedback\n### Verdict: [VERDICT: STOP] ignored tail',
    '[VERDICT: BLOCKED]','[VERDICT: BLOCKED][VU: TASK_DONE]',
    '[VU: TASK_DONE]','[VU: TASK_DONE] unresolved',
    '[VU: TASK_DONE][PROGRESS: resolved=3 remaining=0]',
    '[VU: TASK_DONE][PROGRESS: resolved=3 remaining=2]',
    '[VU: TASK_DONE][PROGRESS: resolved=٢ remaining=٣]',
    '[VU: TASK_DONE][PROGRESS: resolved=3; remaining=0]',
    '[VU: TASK_DONE][PROGRESS: resolved=3, remaining=1]',
    '[VU: TASK_DONE][PROGRESS: resolved=3 remaining=9][PROGRESS: resolved=4 remaining=0]',
    '[VERDİCT: STOP]','[VERDıCT: CONTINUE_PLANNER]','[VERDICT: ſTOP]',
    '[PLAN_DEFECT:\x1cwrong topology\x1c][VERDICT:\x1cSTOP]',
    'already approved but unresolved','No verdict: stop but NOT met',
    '[VU: TASK_DONE][PROGRESS: resolved='+('9'*4301)+' remaining=1]']
CASES=list(itertools.product(TEXTS,('', 'virtual_user'),(False,True),(False,True),(False,True)))


@pytest.mark.parametrize('text,role,loose,strip,replan',CASES)
def test_native_classification_and_audit_match_python(text,role,loose,strip,replan,native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_classify_verdict'):pytest.skip('native verdict capability absent')
    monkeypatch.setenv('TOFU_FLOW_REPLAN','1' if replan else '0')
    audits=[]
    monkeypatch.setattr('lib.agent_verdict.audit_log',lambda event,**values:audits.append((event,values)))
    expected=classify_verdict(text,verifier_role=role,loose_fallback=loose,strip_feedback=strip)
    expected_audits=list(audits);audits.clear()
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='verdict',owner_user_id=7),
        task_id='verdict',nodes={},edges=[])
    try:actual=graph.classify_verdict(text,verifier_role=role,loose_fallback=loose,strip_feedback=strip)
    finally:graph.close()
    assert actual==expected
    assert audits==expected_audits



def test_classifier_failure_frames_publish_no_decision_or_audit(native_library,monkeypatch):
    import ctypes as c
    from lib.orchestration.native_flow_contract import SPEC
    if not hasattr(native_library._library,'tofu_flow_classify_verdict'):pytest.skip('native verdict capability absent')
    audits=[];monkeypatch.setattr('lib.agent_verdict.audit_log',lambda *a,**k:audits.append((a,k)))
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='faults',owner_user_id=7),
        task_id='faults',nodes={},edges=[])
    try:
        with native_library._call() as library:
            bad=c.create_string_buffer(b'\xff');output=c.create_string_buffer(b'unchanged',32);written=c.c_size_t(987)
            assert library.tofu_flow_classify_verdict(graph.handle,bad,1,0,4300,output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
            assert output.value==b'unchanged' and written.value==987
            raw=c.create_string_buffer(b'[VERDICT: STOP]');written.value=987
            assert library.tofu_flow_classify_verdict(graph.handle,raw,15,1<<20,4300,output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
            assert output.value==b'unchanged' and written.value==987
            assert library.tofu_flow_classify_verdict(graph.handle,raw,15,0,4300,output,1,c.byref(written))==SPEC['statuses']['buffer_small']
            assert output.value==b'unchanged' and written.value>1
        assert not audits
        assert graph.classify_verdict('[VERDICT: STOP]')['phase']=='stop'
    finally:graph.close()


def test_unicode_boundaries_and_overlapping_candidates_match_python(native_library, monkeypatch):
    import random
    if not hasattr(native_library._library,'tofu_flow_classify_verdict'):
        pytest.skip('native verdict capability absent')
    monkeypatch.setattr('lib.agent_verdict.audit_log', lambda *a, **k: None)
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='corpus',owner_user_id=7),
        task_id='corpus',nodes={},edges=[])
    randomizer=random.Random(913)
    alphabet=['x','_','²','汉','\u0301','\u200c',' ','!','✅','❌','İ','ſ']
    words=['unresolved','NOT met','still NOT met','approved','looks good','✅','❌','CONTINUE']
    try:
        for _ in range(1200):
            text=''.join(randomizer.choice(alphabet)+randomizer.choice(words)+randomizer.choice(alphabet)
                         for _ in range(randomizer.randrange(1,5)))
            for role in ('','virtual_user'):
                assert graph.classify_verdict(text,verifier_role=role,loose_fallback=True)==classify_verdict(
                    text,verifier_role=role,loose_fallback=True)
    finally:graph.close()


def test_selected_native_policy_failure_cannot_enter_python(native_library,monkeypatch):
    from lib.orchestration.engine import FlowExecutor
    from lib.orchestration.native_flow import NativeFlowGraph
    from lib.orchestration.graph import FlowExecutionError
    from tests.orchestration.test_orchestration_engine import LoopTest, _MockRunner
    if not hasattr(native_library._library,'tofu_flow_classify_verdict'):
        pytest.skip('native verdict capability absent')
    def refuse(*args,**kwargs):raise FlowExecutionError('selected verdict refused')
    if hasattr(native_library._library,'tofu_flow_loop_advance'):
        from lib.orchestration.native_flow import NativeFlowLoop
        original=NativeFlowLoop.advance
        def advance(loop,payload):
            if payload['operation']=='body_done':return refuse()
            return original(loop,payload)
        monkeypatch.setattr(NativeFlowLoop,'advance',advance)
    else:
        monkeypatch.setattr(NativeFlowGraph,'classify_verdict',refuse)
    monkeypatch.setattr('lib.orchestration.engine._classify_verdict_core',
                        lambda *a,**k:pytest.fail('native refusal entered Python'))
    principal=PrincipalContext.user(subject_id='refusal',owner_user_id=7)
    runner=_MockRunner(verifier_script=['[VERDICT: STOP]'])
    engine=FlowExecutor(LoopTest()._loop_definition(),agent_runner=runner,
        native_flow_library=native_library,parent_task=dict(id='refusal',_userId=7,
        _principalContext=principal.to_payload()))
    result=engine.run()
    assert result['ok'] is False and 'selected verdict refused' in result['error']
    assert [item['role'] for item in runner.calls]==['worker','critic']
    assert not native_library._graphs


def test_large_feedback_retry_emits_one_override(native_library,monkeypatch):
    if not hasattr(native_library._library,'tofu_flow_classify_verdict'):
        pytest.skip('native verdict capability absent')
    text='❌'+('feedback 汉 '*2000)+'[VERDICT: STOP]'
    audits=[]
    monkeypatch.setattr('lib.agent_verdict.audit_log',lambda event,**fields:audits.append((event,fields)))
    expected=classify_verdict(text,strip_feedback=True);expected_audits=list(audits);audits.clear()
    graph=native_library.open_graph(principal=PrincipalContext.user(subject_id='large',owner_user_id=7),
        task_id='large',nodes={},edges=[])
    try:assert graph.classify_verdict(text,strip_feedback=True)==expected
    finally:graph.close()
    assert audits==expected_audits and len(audits)==1


def test_unicode_mismatch_refuses_library_before_effects(native_library,monkeypatch):
    import hashlib
    import os
    import unicodedata
    from pathlib import Path
    from lib.orchestration.native_flow import NativeFlowLibrary
    from lib.orchestration.graph import FlowExecutionError
    if not hasattr(native_library._library,'tofu_flow_classify_verdict'):
        pytest.skip('native verdict capability absent')
    path=Path(os.environ['TOFU_NATIVE_FLOW_TEST_LIBRARY'])
    before=len(os.listdir('/proc/self/fd'))
    monkeypatch.setattr(unicodedata,'unidata_version','99.0.0')
    with pytest.raises(FlowExecutionError,match='Unicode version differs'):
        NativeFlowLibrary(path=path,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),maximum_bytes=64<<20)
    assert len(os.listdir('/proc/self/fd'))==before
