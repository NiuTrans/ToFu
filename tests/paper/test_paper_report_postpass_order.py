"""Post-report stages keep their order, exact inputs and best-effort boundaries."""

import pytest

from tests.paper.test_paper_report_body_projection import FINAL_BODY, report_case as report_case

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failed_stage', [None, 'insight', 'termfill', 'checkpoints'])
def test_report_postpasses_continue_after_individual_failures(
        report_case, monkeypatch, caplog, failed_stage):
    from lib.paper.report_engine import _hooks

    run, task, worker = report_case
    calls = []
    inputs = []
    source_inputs = []
    audit_inputs = []
    report_meta = {'terminologyAudit': {'gaps': ['term']}}
    monkeypatch.setattr(worker, '_build_report_meta', lambda *args: report_meta)

    def record(name, carrier, phash, lang, body, model):
        calls.append(name)
        inputs.append((carrier, carrier['status'], phash, lang, body, model))
        if name == failed_stage:
            raise ValueError(f'{name} controlled failure')

    def insight(carrier, phash, lang, body, *, truncated_paper, model):
        source_inputs.append(truncated_paper)
        # Later passes retain their original audit input even when a preceding
        # pass adopts a newer accumulated accounting snapshot on the carrier.
        carrier['report_meta'] = {'secondPasses': {'insight': {'tokens': 1}}}
        record('insight', carrier, phash, lang, body, model)

    def termfill(carrier, phash, lang, body, original_meta, *, model):
        audit_inputs.append(original_meta)
        record('termfill', carrier, phash, lang, body, model)

    def checkpoints(carrier, phash, lang, body, *, model):
        record('checkpoints', carrier, phash, lang, body, model)

    monkeypatch.setattr(worker, '_maybe_run_insight', insight)
    monkeypatch.setattr(worker, '_maybe_run_termfill', termfill)
    monkeypatch.setattr(_hooks, '_maybe_run_checkpoints', checkpoints)

    def dispatch(messages, *, on_content, **kwargs):
        on_content(FINAL_BODY)
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    task, published = run(dispatch)
    assert calls == ['insight', 'termfill', 'checkpoints']
    assert source_inputs == ['source paper']
    assert len(audit_inputs) == 1 and audit_inputs[0] is report_meta
    for carrier, status, phash, lang, body, model in inputs:
        assert carrier is task and status == 'done'
        assert (phash, lang, body, model) == (task['paper_hash'], 'en', FINAL_BODY, 'model-a')
    assert task['status'] == 'done' and len(published) == 1
    assert len([e for e in task['events'] if e['type'] == 'done']) == 1
    if failed_stage:
        assert f'{failed_stage} controlled failure' in caplog.text
    assert sum('second-pass wrapper failed' in record.message for record in caplog.records) == bool(failed_stage)


@pytest.mark.parametrize('request_local', [False, True])
def test_report_postpass_process_control_is_not_swallowed(
        report_case, monkeypatch, request_local):
    from lib.paper.report_engine import _hooks

    run, task, worker = report_case
    if request_local:
        task['requestPolicyV1']['cacheMode'] = 'request_local'
    calls = []

    def interrupted(*args, **kwargs):
        calls.append('insight')
        raise KeyboardInterrupt('controlled interrupt')

    monkeypatch.setattr(worker, '_maybe_run_insight', interrupted)
    monkeypatch.setattr(worker, '_maybe_run_termfill', lambda *a, **k: calls.append('termfill'))
    monkeypatch.setattr(_hooks, '_maybe_run_checkpoints', lambda *a, **k: calls.append('checkpoints'))

    def dispatch(messages, *, on_content, **kwargs):
        on_content(FINAL_BODY)
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    if request_local:
        _, published = run(dispatch)
        assert not calls and not published
    else:
        with pytest.raises(KeyboardInterrupt, match='controlled interrupt'):
            run(dispatch)
        assert calls == ['insight']
    assert task['status'] == 'done'


def test_checkpoint_port_is_resolved_when_its_stage_begins(report_case, monkeypatch):
    from lib.paper.report_engine import _hooks

    run, _, worker = report_case
    calls = []
    monkeypatch.setattr(_hooks, '_maybe_run_checkpoints', lambda *a, **k: calls.append('stale'))

    def insight(*args, **kwargs):
        calls.append('insight')
        monkeypatch.setattr(_hooks, '_maybe_run_checkpoints', lambda *a, **k: calls.append('current'))

    monkeypatch.setattr(worker, '_maybe_run_insight', insight)
    monkeypatch.setattr(worker, '_maybe_run_termfill', lambda *a, **k: calls.append('termfill'))

    def dispatch(messages, *, on_content, **kwargs):
        on_content(FINAL_BODY)
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    task, published = run(dispatch)
    assert calls == ['insight', 'termfill', 'current']
    assert task['status'] == 'done' and len(published) == 1


def test_postpass_input_failure_does_not_skip_later_stages(report_case, monkeypatch, caplog):
    from lib.paper.report_engine import _hooks

    run, _, worker = report_case
    calls = []
    monkeypatch.setattr(worker, '_maybe_run_insight', lambda *a, **k: calls.append('insight'))
    monkeypatch.setattr(worker, '_maybe_run_termfill', lambda *a, **k: calls.append('termfill'))
    monkeypatch.setattr(_hooks, '_maybe_run_checkpoints', lambda *a, **k: calls.append('checkpoints'))

    def dispatch(messages, *, on_content, **kwargs):
        # Fault injection: source projection disappears after primary dispatch.
        messages[1].pop('content')
        on_content(FINAL_BODY)
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    task, published = run(dispatch)
    assert calls == ['termfill', 'checkpoints']
    assert task['status'] == 'done' and len(published) == 1
    assert '[Paper:Insight] second-pass wrapper failed' in caplog.text
