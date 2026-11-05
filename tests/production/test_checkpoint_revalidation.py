"""Restarted production validates retained artifacts before adopting them."""
import threading

import pytest

from lib.production import stages as owner

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('parallel', [False, True])
def test_missing_output_is_rebuilt_before_a_dependent_checkpoint(tmp_path, parallel):
    state_path = str(tmp_path / 'stages.json')
    output = tmp_path / 'report.txt'
    calls = []
    def produce(_ctx):
        calls.append('produce')
        output.write_text('verified report')
        return {'path': str(output)}
    stage = owner.Stage('report', produce, gate=lambda _ctx, _artifact: [] if output.is_file() else ['missing report'])
    dependent = owner.Stage('export', lambda ctx: calls.append('export') or {'source': ctx['artifacts']['report']['path']})
    def run():
        context = {}
        if parallel:
            owner.run_independent_stages([stage], context, state_path=state_path,
                                         dependent_stage_names=['export'])
            return owner.run_stages([dependent], context, state_path=state_path)
        return owner.run_stages([stage, dependent], context, state_path=state_path)
    run()
    output.unlink()
    run()
    assert calls == ['produce', 'export', 'produce', 'export']
    assert output.read_text() == 'verified report'


@pytest.mark.parametrize('parallel', [False, True])
def test_checkpoint_validator_failure_never_reexecutes_the_producer(tmp_path, parallel):
    state_path = str(tmp_path / 'stages.json')
    calls, fail = [], threading.Event()
    def validate(_ctx, _artifact):
        if fail.is_set():
            raise OSError('evidence temporarily unreadable')
        return []
    stage = owner.Stage('report', lambda _ctx: calls.append('effect') or {'value': 'original'}, gate=validate, retry=2)
    run = owner.run_independent_stages if parallel else owner.run_stages
    run([stage], {}, state_path=state_path)
    before = (tmp_path / 'stages.json').read_bytes()
    fail.set()
    with pytest.raises(owner.StageFailed, match='checkpoint verification failed'):
        run([stage], {}, state_path=state_path)
    assert calls == ['effect']
    assert (tmp_path / 'stages.json').read_bytes() == before
    fail.clear()
    assert run([stage], {}, state_path=state_path)['report']['value'] == 'original'
    assert calls == ['effect']


@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('stop_at', ['producer', 'validator'])
def test_last_stage_observes_stop_before_publishing_success(tmp_path, parallel, stop_at):
    aborted = threading.Event()
    events = []
    def produce(_ctx):
        if stop_at == 'producer':
            aborted.set()
        return {'value': 'unfinished'}
    def validate(_ctx, _artifact):
        if stop_at == 'validator':
            aborted.set()
        return []
    run = owner.run_independent_stages if parallel else owner.run_stages
    with pytest.raises(owner.StageAborted):
        run([owner.Stage('report', produce, gate=validate)], {},
            state_path=str(tmp_path / 'stages.json'), abort_check=aborted.is_set,
            emit=events.append)
    assert not owner.stage_is_done(owner.load_state(str(tmp_path / 'stages.json')), 'report')
    assert not any(event['type'] == 'stage_done' for event in events)
