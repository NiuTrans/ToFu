#!/usr/bin/env python3
"""The persisted research artifacts must be REACHABLE — over HTTP, by direction.

THE DEFECT THIS PINS (epic pt_a40dbd9569194b52, second half).
The first half gave research artifacts a durable home in ``paper_reports``.
But durability with no read path is a warehouse with no door: the artifacts
sat on disk while the product had no way to ask for them.

  * ``GET /api/v1/tasks/<id>`` resolves against the IN-MEMORY task registry,
    so the moment ``cleanup_stale()`` evicts the task (TTL) or the process
    restarts, it 404s — precisely the window the persistence exists for.
  * The persisted row is keyed by DIRECTION hash and carries no task id, so
    even a remembered task id could not address it.

★ THE RECURRING FAILURE SHAPE THIS FILE EXISTS TO BREAK
Twice now this capability shipped a function that was written, exported, and
never called: first ``survey_lang_key`` / ``ideate_lang_key`` (0 callers while
the sibling ``insight_lang_key`` had 19), then ``load_research_artifacts``.
A guard asserting "the symbol exists" would have passed in BOTH cases.

So every assertion here drives the REAL Quart app through the REAL HTTP stack
and asserts what a CLIENT receives. The decisive test
(:func:`test_direction_lookup_works_with_no_live_task_at_all`) empties the task
registry first, so it can only pass if the response was served from durable
storage. A mocked engine or a bare symbol import cannot satisfy it.

Run:  pytest tests/test_research_lookup_route.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _new_loop_run(coro):
    """Drive one coroutine on a private loop.

    The project convention for async route tests (see
    tests/test_agent_poll_routes.py): the suite runs with
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, so pytest-asyncio is NOT loaded and a
    ``@pytest.mark.asyncio`` test would be silently skipped rather than run —
    a guard that never executes is worse than no guard.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

_DIRECTION = 'long-context KV-cache compression'

_OPEN_GAPS = {
    'schema_version': 1, 'direction': _DIRECTION, 'lang': 'en',
    'surveyed_count': 18,
    'open_gaps': [{'id': 'gap_1', 'gap': 'no method preserves needle recall',
                   'why_open': 'only perplexity is reported',
                   'evidence': ['2305.11111'], 'kind_hint': 'methodology'}],
}

_IDEATE = {
    'accepted': [{
        'id': 'idea_1', 'title': 'Per-layer learnable compression rate',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'core_mechanism': 'attention entropy varies per layer',
        'novelty_claim': 'unlike 2305.11111 the rate is learned',
        'falsifiable_prediction': 'needle recall drops <2% at 4x',
        'why_not_AB': 'derived from the spectrum, not bolted on',
        'scores': {'novelty': 4, 'falsifiability': 5,
                   'mechanism_depth': 4, 'value': 4},
        'overall': 4.25}],
    'rejected': [{
        'id': 'idea_2', 'title': 'KV compression + speculative decoding',
        'reject_stage': 'rubric', 'reject_reason': 'overall 2.75 < 4.0',
        'scores': {'novelty': 2, 'falsifiability': 3,
                   'mechanism_depth': 2, 'value': 4},
        'overall': 2.75}],
    'threshold': 4.0, 'gate_reached': 'accepted',
}


# ── Fixtures: the REAL app, the REAL DB ────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    from lib.database import reset_sqlite_for_tests, restore_db_state
    snapshot = reset_sqlite_for_tests(str(tmp_path / 'research_route.db'))
    try:
        yield
    finally:
        restore_db_state(snapshot)


@pytest.fixture()
def client(fresh_db):
    """A real test client over the real blueprint registration.

    Deliberately NOT a hand-built mini-app: registering the blueprint by hand
    here would let a route that is never mounted in production still pass.
    """
    import server
    app = server.app
    app.config['TESTING'] = True
    return app.test_client()


@pytest.fixture()
def seeded(fresh_db):
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(_DIRECTION, 'en', '# Survey\n\nBody.', _OPEN_GAPS, model='m1')
    persist_ideate(_DIRECTION, 'en', _IDEATE, model='m1')


def _empty_the_task_registry():
    """Evict every research task — what cleanup_stale() does past TTL and
    what a process restart does absolutely."""
    from lib.research.runtime import _research_runtime
    with _research_runtime._lock:
        _research_runtime._tasks.clear()


def _get_json(client, path, **query):
    resp = _new_loop_run(client.get(path, query_string=query))
    return resp.status_code, _new_loop_run(resp.get_json())


# ── 1. ★ The decisive criterion ────────────────────────────────────────────

def test_direction_lookup_works_with_no_live_task_at_all(client, seeded):
    """A finished job must stay readable after its task is gone.

    This is the whole point of the persistence layer: the registry is empty
    (TTL swept / server restarted), so a response can ONLY come from disk.
    """
    _empty_the_task_registry()

    status, body = _get_json(
        client, '/api/v1/research/lookup', direction=_DIRECTION, lang='en')

    assert status == 200, f'lookup failed with no live task: {status} {body}'
    assert body.get('found') is True, (
        'the persisted research was NOT reachable once the in-memory task was '
        f'evicted — the durable read path does not work: {body}')
    assert body.get('survey_md', '').startswith('# Survey')
    assert body['open_gaps']['open_gaps'][0]['id'] == 'gap_1'
    acc = body.get('accepted') or []
    assert len(acc) == 1 and acc[0]['core_mechanism'], \
        'accepted ideas came back without their mechanism text'
    rej = body.get('rejected') or []
    assert rej and rej[0]['scores']['novelty'] == 2, (
        'the four-axis rubric scores did not survive the HTTP round-trip — '
        'they are the calibration data for IDEATE_GATE_THRESHOLD')
    assert body.get('threshold') == 4.0
    assert body.get('gate_reached') == 'accepted'


def test_lookup_is_case_and_whitespace_insensitive_over_http(client, seeded):
    """A human retypes the direction. It must still hit the stored row —
    a miss re-runs the whole pipeline at full cost."""
    _empty_the_task_registry()
    status, body = _get_json(
        client, '/api/v1/research/lookup',
        direction='  Long-Context  KV-Cache Compression  ', lang='en')
    assert status == 200 and body.get('found') is True, (
        f'a retyped direction missed the persisted row: {body}')


# ── 2. Honest empties + input validation ──────────────────────────────────

def test_unresearched_direction_is_an_honest_empty_not_a_404(client):
    """The re-attach path calls this on every open. A never-researched
    direction is a normal answer (found=false), not an error."""
    status, body = _get_json(
        client, '/api/v1/research/lookup', direction='never researched', lang='en')
    assert status == 200, f'expected 200 honest-empty, got {status}'
    assert body.get('found') is False
    assert body.get('accepted') == [] and body.get('survey_md') == ''


def test_missing_direction_is_rejected(client):
    status, _ = _get_json(client, '/api/v1/research/lookup', lang='en')
    assert status == 400, 'a lookup with no direction must be a 400'


def test_languages_are_separate_over_http(client, seeded):
    """`seeded` wrote only 'en'. Asking for 'zh' must not serve the English
    row — the composite key is per-language."""
    _empty_the_task_registry()
    _, en = _get_json(client, '/api/v1/research/lookup',
                      direction=_DIRECTION, lang='en')
    _, zh = _get_json(client, '/api/v1/research/lookup',
                      direction=_DIRECTION, lang='zh')
    assert en.get('found') is True
    assert zh.get('found') is False, 'the zh lookup served the en row'


# ── 3. Degraded must stay visible through the read path ───────────────────

def test_degraded_flag_survives_the_persist_and_the_lookup(client, fresh_db):
    """A degraded run keeps status='done' by design, so if the quality flag
    is dropped anywhere along persist→load→HTTP, a 100%% gate wipe reads as a
    clean success on re-open."""
    from lib.research.persistence import persist_ideate
    wiped = dict(_IDEATE, accepted=[], gate_reached='structural',
                 degraded=True,
                 degraded_reason='structural gate rejected ALL 6 idea(s)')
    persist_ideate(_DIRECTION, 'en', wiped, model='m1')
    _empty_the_task_registry()

    status, body = _get_json(
        client, '/api/v1/research/lookup', direction=_DIRECTION, lang='en')
    assert status == 200
    assert body.get('degraded') is True, (
        'a degraded run reads back as a clean success — the quality axis was '
        'lost between the DB and the client')
    assert 'structural gate' in (body.get('degraded_reason') or '')


# ── 4. The wiring pins: a real caller, a real mount ───────────────────────

def test_route_is_mounted_via_normal_blueprint_registration(client):
    """The route must reach clients through the app's own registration, not
    a test-only mount."""
    import server
    rules = {str(r) for r in server.app.url_map.iter_rules()}
    assert '/api/v1/research/lookup' in rules, (
        'the lookup route is not on the real app url_map — it would 404 in '
        f'production. Mounted /api/v1/research/*: '
        f'{[r for r in rules if "research" in r]}')


def test_the_persistence_loader_has_a_real_caller():
    """★ The anti-orphan pin.

    `load_research_artifacts` was the SECOND function in this epic to be
    written, exported, and never called. Assert it is imported by a ROUTE
    module (production code, not tests) — the check that would have caught
    both instances.
    """
    from tests._source_scan import strip_comments  # shared scanner (charter #24)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = strip_comments(
        open(os.path.join(root, 'routes', 'api_v1', 'research.py'),
             encoding='utf-8').read(), lang='python')
    assert 'load_research_artifacts' in src, (
        'no route calls load_research_artifacts — the persisted artifacts are '
        'unreachable from the product, which is the exact orphan shape this '
        'epic has already produced twice')


def test_frontend_client_exposes_the_lookup():
    """api.js is the single seam for backend calls (charter: no raw fetch
    outside it). A route with no client method is unreachable from the UI."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'static', 'js', 'api.js'),
               encoding='utf-8').read()
    assert '/api/v1/research/lookup' in src, (
        'Api has no method for the research lookup — the frontend cannot '
        'reach the persisted artifacts')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
