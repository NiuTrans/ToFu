"""Operation diagnostics retain only canonical labels and delegate exact effects."""
import pytest
from benchmarks.native_application_stages import Stages

pytestmark=pytest.mark.unit


def test_storage_labels_preserve_call_identity_and_terminal_phase():
    owner,payload,result=object(),{'secret':'private'},object()
    calls=[]
    def original(*args,**kwargs):
        calls.append((args,kwargs));return result
    stages=Stages();wrapped=stages.storage_v2_attempt(original)
    with stages.span('native.terminal_writer'):
        assert wrapped(owner,'task_results.checkpoint',payload,command_id='private-id') is result
    assert calls[0][0]==(owner,'task_results.checkpoint',payload)
    row=stages.rows[0]
    assert row['name']=='storage.v2.attempt'
    assert row['operation']=='task_results.checkpoint' and row['phase']=='native.terminal_writer'
    assert 'private' not in str(stages.rows)


def test_unknown_operation_is_redacted_and_failure_resets_phase():
    stages=Stages();failure=RuntimeError('private-error')
    def original(*args,**kwargs):raise failure
    wrapped=stages.storage_v2_attempt(original)
    with pytest.raises(RuntimeError) as caught:
        with stages.span('native.context_prepare'):
            wrapped(object(),operation='secret-content',payload={'secret':'private'})
    assert caught.value is failure
    assert stages.rows[0]['operation']=='uncataloged'
    assert stages.rows[0]['phase']=='native.context_prepare'
    assert 'secret' not in str(stages.rows) and 'private' not in str(stages.rows)
    with pytest.raises(RuntimeError):wrapped(object(),'task_results.replay_get',{})
    assert stages.rows[-1]['phase']=='other'


def test_fixture_poll_phase_propagates_to_joined_off_loop_read():
    import asyncio
    stages=Stages();wrapped=stages.storage_v2_attempt(lambda *args,**kwargs: 'returned')
    async def run():
        with stages.span('fixture.attempt_poll',cpu=False):
            assert await asyncio.to_thread(wrapped,object(),'turn.attempt.get',{})=='returned'
    asyncio.run(run())
    assert stages.rows[0]['phase']=='fixture.attempt_poll'
    assert stages.rows[0]['operation']=='turn.attempt.get'
