"""Tests for Producer A — write_file → chat artifact promotion (Step 2).

The hook lives in ``lib/tasks_pkg/handlers/project.py`` as
``_maybe_promote_write_to_artifact``.  It is invoked at the end of the
project tool handler for ``write_file``/``apply_diff``/``insert_content``,
reads the on-disk file back, and (when the path is renderable) creates a
chat_artifacts row + emits an artifact SSE event.

We test the hook directly with a real project root + real disk write so
the round-trip exercises ``_resolve_write_path`` + the artifact subsystem.

Run:  pytest tests/test_artifacts_producer_write_file.py -v
"""
from __future__ import annotations

import os
import tempfile
import threading

import pytest

pytest_plugins = ('tests._artifact_sidecar',)


@pytest.fixture()
def tmp_project_root(tmp_path):
    """Register a tmp dir as a workspace root so write_file accepts paths."""
    from lib.project_mod.scanner import add_project_root
    from lib.project_mod.config import _state, _lock

    root = str(tmp_path)
    # The project_mod state machine requires a primary project to exist
    # before secondary roots are accepted.  Set one if not already set.
    with _lock:
        if not _state.get('path'):
            _state['path'] = root
    add_project_root(root, name='tofu-tests')
    return root


@pytest.fixture()
def fake_task():
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    task = {
        'id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        'convId': 'conv-producer',
        '_userId': 1,
        'status': 'running',
        'config': {'userId': 1},
        'events': [],
        'events_lock': threading.Lock(),
    }
    with tasks_lock:
        tasks[task['id']] = task
    try:
        yield task
    finally:
        with tasks_lock:
            tasks.pop(task['id'], None)


def _last_artifact_event(task):
    """Return the most recent 'artifact' event, or None."""
    for ev in reversed(task['events']):
        if ev.get('type') == 'artifact':
            return ev
    return None


# ─── _maybe_promote_write_to_artifact ───────────────────────────────

def test_skips_md_write(flask_app, tmp_project_root, fake_task):
    """Routine project markdown edits (README.md, etc.) must NOT spawn
    artifact chips.  Inline ```markdown fences still go through the
    scanner (Producer B); this test only covers the write_file path."""
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    rel = 'report.md'
    body = '# Report\n\nHello world.\n'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write(body)

    meta = {'toolName': 'write_file', 'title': 'write_file'}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta,
        )

    assert 'artifactId' not in meta
    assert _last_artifact_event(fake_task) is None


def test_promotes_html_write_full_event(flask_app, tmp_project_root, fake_task):
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    rel = 'report.html'
    body = '<!doctype html><html><body><h1>Report</h1><p>Hi.</p></body></html>\n'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write(body)

    meta = {'toolName': 'write_file', 'title': 'write_file'}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta,
        )

    assert meta.get('artifactId'), 'artifactId should be attached to meta'
    assert meta.get('artifactFormat') == 'html'
    assert meta.get('artifactTitle') == 'report.html'
    assert meta.get('artifactSize') == len(body.encode('utf-8'))

    ev = _last_artifact_event(fake_task)
    assert ev is not None, 'an artifact SSE event should have been emitted'
    assert ev['id'] == meta['artifactId']
    assert ev['format'] == 'html'
    assert ev['source'] == 'write_file'
    assert ev['url'] == f'/api/artifacts/{ev["id"]}/raw'
    # Defense-in-depth: content blob MUST NOT be in the SSE event
    assert 'content' not in ev


def test_promotes_html_write(flask_app, tmp_project_root, fake_task):
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact
    from lib.artifacts import get_artifact

    rel = 'page.html'
    body = '<!doctype html><html><body><h1>Hi</h1></body></html>\n'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write(body)

    meta = {'toolName': 'write_file', 'title': 'write_file'}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta,
        )
        # Round-trip: the persisted content equals what we wrote.
        full = get_artifact(meta['artifactId'])

    assert meta.get('artifactFormat') == 'html'
    assert full['content'] == body
    assert full['source'] == 'write_file'
    assert full['source_ref'] == {'path': rel, 'tool': 'write_file'}


def test_skips_non_renderable(flask_app, tmp_project_root, fake_task):
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    rel = 'script.py'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write('print("hi")\n')

    meta = {'toolName': 'write_file'}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta,
        )

    # No artifact metadata mutated, no SSE event.
    assert 'artifactId' not in meta
    assert _last_artifact_event(fake_task) is None


def test_skips_when_file_missing(flask_app, tmp_project_root, fake_task):
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    meta = {'toolName': 'write_file'}
    with flask_app.app_context():
        # File does not exist on disk — handler degrades gracefully.
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': 'nope.html'}, tmp_project_root, meta,
        )
    assert 'artifactId' not in meta
    assert _last_artifact_event(fake_task) is None


def test_size_cap_enforced(flask_app, tmp_project_root, fake_task):
    from lib.tasks_pkg.handlers.project import (
        _WRITE_ARTIFACT_MAX_BYTES,
        _maybe_promote_write_to_artifact,
    )

    rel = 'huge.html'
    big = '<!doctype html>' + ('a' * (_WRITE_ARTIFACT_MAX_BYTES + 1))
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write(big)

    meta = {'toolName': 'write_file'}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta,
        )

    # Above the per-feature cap → no artifact, no event.
    assert 'artifactId' not in meta
    assert _last_artifact_event(fake_task) is None


def test_apply_diff_promotes(flask_app, tmp_project_root, fake_task):
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    rel = 'edited.html'
    body = '<!doctype html><html><body>Edited</body></html>\n'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write(body)

    meta = {'toolName': 'apply_diff'}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'apply_diff', {'path': rel}, tmp_project_root, meta,
        )

    assert meta.get('artifactId')
    assert meta.get('artifactFormat') == 'html'


def test_dedupe_idempotent_repeat_writes(flask_app, tmp_project_root, fake_task):
    """Writing the same content twice should reuse the same artifact id."""
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    rel = 'idem.html'
    body = '<!doctype html><html><body>Idempotent</body></html>\n'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write(body)

    meta_a, meta_b = {}, {}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta_a,
        )
        _maybe_promote_write_to_artifact(
            fake_task, 'write_file', {'path': rel}, tmp_project_root, meta_b,
        )

    assert meta_a['artifactId'] == meta_b['artifactId']


def test_no_conv_id_skips(flask_app, tmp_project_root):
    from lib.tasks_pkg.handlers.project import _maybe_promote_write_to_artifact

    rel = 'no-conv.html'
    with open(os.path.join(tmp_project_root, rel), 'w') as f:
        f.write('<!doctype html><html><body>x</body></html>\n')

    meta = {}
    task = {'id': 'tid', 'convId': '', 'events': [],
            'events_lock': threading.Lock()}
    with flask_app.app_context():
        _maybe_promote_write_to_artifact(
            task, 'write_file', {'path': rel}, tmp_project_root, meta,
        )
    assert 'artifactId' not in meta
