"""Own isolated report workers, synthetic provider seams, and their test lifecycle."""

from contextlib import contextmanager
from uuid import uuid4


@contextmanager
def isolated_report_case(monkeypatch):
    """Retire the task here; the supplied patch scope owns all seam restoration.

    Fixture setup and caller overrides must share that scope so teardown cannot
    restore an already-retired fixture seam. Nest explicit MonkeyPatch contexts
    when independent report cases must restore their enclosing case.
    """
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.paper import report_runtime
    from lib.paper.report_engine import worker
    from lib.paper import citation_audit
    from lib.paper.terminology_audit import audit

    runtime = TaskRuntime('paper-report-projection-test')
    monkeypatch.setattr(report_runtime, '_report_runtime', runtime)
    monkeypatch.setattr(report_runtime, '_report_dedup_index', {})
    monkeypatch.setattr(worker, '_report_runtime', runtime)
    monkeypatch.setattr(worker, 'lookup_paper_title', lambda *a, **k: '')
    monkeypatch.setattr(worker, 'extract_title_from_report', lambda text: '')
    monkeypatch.setattr(worker, 'inject_images_into_report', lambda text, *a, **k: text)
    monkeypatch.setattr(citation_audit, 'build_citation_audit', lambda body: None)
    monkeypatch.setattr(audit, 'build_terminology_audit', lambda body: None)
    monkeypatch.setattr(worker, '_build_report_meta', lambda *a: {})
    monkeypatch.setattr(worker, 'execute_paper_tool',
                        lambda *a, **k: ('evidence', [], None, None, None))
    published = []

    class Repository:
        def __init__(self, owner):
            assert owner == 17

        def put_report(self, report, **kwargs):
            published.append(report)
            return True

    monkeypatch.setattr(worker, 'PaperArtifactRepository', Repository)
    task = report_runtime._new_report_task(
        f'projection-{uuid4().hex}', 'projection-paper', 'en', 'model-a', user_id=17,
        config={'paperInsightEnabled': False, 'paperTermfillEnabled': False,
                'paperCheckpointsEnabled': False})

    def run(dispatch):
        monkeypatch.setattr(worker, 'dispatch_stream', dispatch)
        worker.run_report_task(task, [
            {'role': 'system', 'content': 'policy'},
            {'role': 'user', 'content': 'source paper'},
        ], [])
        return task, published

    try:
        yield run, task, worker
    finally:
        # The caller owns synchronous dispatch and joins any test executor.
        # Retire even a task whose test fails before dispatch or settlement.
        runtime.discard(task['task_id'])
