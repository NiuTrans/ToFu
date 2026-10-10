"""Offline research workbench progress and publication authority tests."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib.research.progress import project_progress


pytestmark = pytest.mark.unit


def test_contract_bounds_unknowns_and_no_false_readiness():
    import jsonschema
    value = project_progress(None, {'type': 'search_round', 'counts': {'discovered': 999}})
    assert value['counts']['discovered'] == 200
    assert value['counts']['excerpt_covered'] is None
    assert not value['artifact_ready']
    final = project_progress(value, {'type': 'final', 'accepted': 2, 'rejected': 3})
    assert final['execution_state'] == 'running'  # Generic settlement still owns done.
    assert not final['artifact_ready']
    assert value['counts']['accepted'] is None
    schema = json.loads(Path('contracts/research_progress_v1.schema.json').read_text())
    jsonschema.validate(final, schema)
    assert project_progress(final, {'type': 'stage_skipped', 'stage': 'publish'})['artifact_ready']


def test_generated_contract_is_current():
    result = subprocess.run(
        [sys.executable, 'scripts/generate_research_progress.py', '--check'],
        capture_output=True, text=True)
    assert result.returncode == 0, (
        f'research progress generated contract drifted:\n{result.stdout}{result.stderr}')


def test_terminal_and_cancelled_callbacks_are_fenced():
    from threading import Event
    from lib.research.runtime import _append_research_event
    abort = Event()
    task = {'status': 'done', 'abort_event': abort}
    assert _append_research_event(task, {'type': 'final'}) is None
    task['status'] = 'running'
    abort.set()
    assert _append_research_event(task, {'type': 'paper_done'}) is None


def test_discovery_observations_are_factual_and_bounded():
    from lib.research.discovery import discover_corpus
    events = []
    result = discover_corpus('example', target=3, search=lambda *a, **k: [], on_progress=events.append)
    assert events
    assert len(events) <= 6
    assert all(e['counts']['discovered'] == 0 for e in events)
    assert len(events) == len(result['rounds'])


def test_cancel_before_publication_performs_no_write(monkeypatch):
    from lib.research import recipe
    from lib.production.stages import StageAborted
    def forbidden(*args, **kwargs):
        pytest.fail('cancelled publication wrote repository')
    monkeypatch.setattr(recipe, '_persist_survey', forbidden)
    with pytest.raises(StageAborted):
        recipe._run_publish({'user_id': 1, 'direction': 'x', 'abort_check': lambda: True,
                             'artifacts': {'survey': {}, 'ideate': {}, 'evaluate': {}}})


def test_business_clock_and_revision_ignore_heartbeat():
    first = project_progress(None, {'type': 'search_round'}, run_id='run-1', now_ms=1234)
    pulse = project_progress(first, {'type': 'heartbeat'}, run_id='run-1', now_ms=9999)
    assert pulse == first
    assert first['run_id'] == 'run-1'
    next_progress = project_progress(first, {'type': 'paper_done'}, run_id='run-1', now_ms=2345)
    assert next_progress['revision'] == first['revision'] + 1
    assert next_progress['last_business_progress_at'] == 2345
