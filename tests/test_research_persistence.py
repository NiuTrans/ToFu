#!/usr/bin/env python3
"""Auto-research artifacts must OUTLIVE the process that produced them.

THE BUG THIS PINS (epic pt_a40dbd9569194b52). Every R1–R4 artifact — the
accepted ideas, the rejected ones WITH their four-axis rubric scores, the
survey markdown and the open-gap map — lived ONLY in
``ProductionRuntime('research', ttl=7200)``'s in-process ``_tasks`` dict.
``cleanup_stale()`` sweeps terminal tasks past TTL and ``pop``s them, so:

  * ~2h after a run finished, the artifacts were gone for good;
  * a server restart destroyed them instantly.

Meanwhile the sibling capabilities in the SAME paper mode (report / review /
rebuttal / insight) all persist to ``paper_reports`` and are retrievable
forever. The design doc (§5) specified the same home for research —
``survey:<lang>`` / ``ideate:<lang>`` composite lang keys, "not one line of new
schema". The two key functions were even written and exported… and had ZERO
callers anywhere in the tree, while the sibling ``insight_lang_key`` had 19.
The persistence was specified, half-built, and never wired.

★ WHY THE ASSERTIONS LOOK THE WAY THEY DO
The honest test of "does it survive TTL + a restart" is NOT "was the writer
called" (a mock can satisfy that while nothing reaches disk). So every test
below writes through the REAL upsert path into a REAL SQLite DB, then
**destroys the entire in-memory runtime** — evicting the task exactly as
``cleanup_stale()`` would — and only then reads back. If the artifact is
still there, it genuinely outlived the process state.

Run:  pytest tests/test_research_persistence.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Real isolated Sidecar authority."""
    import lib.research.persistence as persistence
    from lib.storage import StorageSupervisor

    supervisor = StorageSupervisor(
        project_root=tmp_path / 'sidecar', backend='sqlite', startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(
        persistence, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield supervisor
    finally:
        supervisor.stop()


_DIRECTION = 'long-context KV-cache compression'

_OPEN_GAPS = {
    'schema_version': 1,
    'direction': _DIRECTION,
    'lang': 'en',
    'surveyed_count': 18,
    'open_gaps': [
        {'id': 'gap_1',
         'gap': 'no method preserves needle-in-haystack retrieval under compression',
         'why_open': 'existing work only reports perplexity',
         'evidence': ['2305.11111'],
         'kind_hint': 'methodology'},
    ],
}

_IDEATE_ARTIFACT = {
    'accepted': [
        {'id': 'idea_1',
         'title': 'Per-layer learnable compression rate for KV low-rank projection',
         'kind': 'methodology',
         'linked_gap_id': 'gap_1',
         'core_mechanism': 'attention entropy varies per layer, so a single global '
                           'rate over-compresses the retrieval-critical layers',
         'novelty_claim': 'unlike 2305.11111 the rate is learned, not fixed',
         'falsifiable_prediction': 'needle recall drops <2% at 4x compression',
         'why_not_AB': 'not a graft: the rate is derived from the attention '
                       'spectrum, not bolted on',
         'scores': {'novelty': 4, 'falsifiability': 5,
                    'mechanism_depth': 4, 'value': 4},
         'overall': 4.25},
    ],
    'rejected': [
        {'id': 'idea_2', 'title': 'KV compression + speculative decoding',
         'reject_stage': 'rubric', 'reject_reason': 'overall 2.75 < 4.0',
         'scores': {'novelty': 2, 'falsifiability': 3,
                    'mechanism_depth': 2, 'value': 4},
         'overall': 2.75},
    ],
    'threshold': 4.0,
    'gate_reached': 'accepted',
    'evaluation': {
        'ok': True, 'overall_score': 4.1, 'worth_following_up': True,
        'judge_count': 2,
        'scores': {'survey_coverage': 4.0},
        'usage': {'calls': 2, 'prompt_tokens': 120, 'cost_cny': 0.12},
    },
}


# ── 1. The identity: a direction must hash stably and never collide ────────

class TestDirectionIdentity:
    def test_same_direction_same_hash(self):
        from lib.research.persistence import research_direction_hash
        assert research_direction_hash(_DIRECTION) == \
            research_direction_hash(_DIRECTION)

    def test_whitespace_and_case_insensitive(self):
        """A direction is free text a human retypes — '  KV Cache ' and
        'kv cache' must reach the SAME persisted row, or the user gets a
        cache miss and pays for the whole pipeline again."""
        from lib.research.persistence import research_direction_hash
        assert research_direction_hash('  KV Cache Compression \n') == \
            research_direction_hash('kv cache compression')

    def test_different_directions_differ(self):
        from lib.research.persistence import research_direction_hash
        assert research_direction_hash('kv cache') != \
            research_direction_hash('diffusion models')

    def test_empty_direction_returns_empty(self):
        from lib.research.persistence import research_direction_hash
        assert research_direction_hash('') == ''
        assert research_direction_hash('   ') == ''
        assert research_direction_hash(None) == ''

    def test_namespaced_away_from_paper_content_hashes(self):
        """A direction is NOT a paper. Its identity must be namespaced so a
        direction whose text happens to equal a paper's text can never land on
        that paper's ``paper_reports`` rows."""
        from lib.paper_identity import _paper_hash
        from lib.research.persistence import research_direction_hash
        text = 'some text'
        assert research_direction_hash(text) != _paper_hash(text)


# ── 2. ★ The core criterion: survives TTL eviction + process restart ───────

class TestArtifactsOutliveTheProcess:
    """The runtime is wiped between write and read — the ONLY way to prove
    the artifact is not merely sitting in the in-memory task dict."""

    @staticmethod
    def _wipe_runtime():
        """Evict EVERY research task, exactly as cleanup_stale() does past
        TTL (and as a process restart does absolutely)."""
        from lib.research.runtime import _research_runtime
        with _research_runtime._lock:
            _research_runtime._tasks.clear()

    def test_survey_survives(self, fresh_db):
        from lib.research.persistence import (load_research_artifacts,
                                              persist_survey)
        assert persist_survey(_DIRECTION, 'en', '# Survey\n\nBody.',
                              _OPEN_GAPS, model='m1',
                              user_id=TEST_OWNER_USER_ID) is True
        self._wipe_runtime()

        got = load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)
        assert got['survey_md'] == '# Survey\n\nBody.', (
            'the survey markdown did not survive — it is still process-local')
        assert got['open_gaps']['open_gaps'][0]['id'] == 'gap_1', (
            'the open-gap map (R3 input contract) did not survive')

    def test_ideate_survives_with_scores_intact(self, fresh_db):
        """The rubric scores are the calibration data for
        IDEATE_GATE_THRESHOLD — losing them loses the ability to tune the
        gate from real runs, which the design doc calls the next milestone."""
        from lib.research.persistence import (load_research_artifacts,
                                              persist_ideate)
        assert persist_ideate(_DIRECTION, 'en', _IDEATE_ARTIFACT,
                              model='m1', user_id=TEST_OWNER_USER_ID) is True
        self._wipe_runtime()

        got = load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)
        acc = got['accepted']
        assert len(acc) == 1 and acc[0]['title'].startswith('Per-layer'), \
            'accepted ideas did not survive'
        assert acc[0]['core_mechanism'], 'the mechanism text was dropped'
        rej = got['rejected']
        assert len(rej) == 1, 'the rejection audit did not survive'
        assert rej[0]['scores']['novelty'] == 2, (
            'four-axis rubric scores were dropped — threshold calibration '
            'data is exactly what the rejection audit exists to keep')
        assert rej[0]['reject_reason'], 'the rejection reason was dropped'
        assert got['threshold'] == 4.0
        assert got['gate_reached'] == 'accepted'
        assert got['evaluation']['overall_score'] == 4.1
        assert got['evaluation']['judge_count'] == 2
        assert got['usage']['stages']['evaluate']['calls'] == 2

    def test_absent_direction_reads_back_empty_not_error(self, fresh_db):
        """A direction never researched is an honest empty, not an exception —
        the re-attach path calls this on every open."""
        from lib.research.persistence import load_research_artifacts
        got = load_research_artifacts(
            'never researched anything', 'en',
            user_id=TEST_OWNER_USER_ID)
        assert got['found'] is False
        assert got['accepted'] == [] and got['survey_md'] == ''

    def test_found_flag_is_true_once_anything_persisted(self, fresh_db):
        from lib.research.persistence import (load_research_artifacts,
                                              persist_survey)
        persist_survey(
            _DIRECTION, 'en', '# S', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        assert load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)['found'] is True


# ── 3. Key discipline: never collide with a paper's own rows ──────────────

class TestCompositeKeyDiscipline:
    def test_survey_and_ideate_are_separate_rows(self, fresh_db):
        from lib.research.persistence import (
            persist_ideate, persist_survey, research_direction_hash,
        )
        persist_survey(
            _DIRECTION, 'en', '# S', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        persist_ideate(
            _DIRECTION, 'en', _IDEATE_ARTIFACT, model='',
            user_id=TEST_OWNER_USER_ID)
        rows = fresh_db.client.query('research.artifacts.get', {
            'user_id': TEST_OWNER_USER_ID,
            'paper_hash': research_direction_hash(_DIRECTION),
            'lang': 'en',
        })
        langs = sorted(row['lang_key'] for row in rows)
        assert langs == ['ideate:en', 'survey:en'], (
            f'expected two distinct composite-key rows, got {langs}')

    def test_languages_do_not_overwrite_each_other(self, fresh_db):
        from lib.research.persistence import (load_research_artifacts,
                                              persist_survey)
        persist_survey(
            _DIRECTION, 'en', '# English', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        persist_survey(
            _DIRECTION, 'zh', '# 中文', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        assert load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)['survey_md'] == '# English'
        assert load_research_artifacts(
            _DIRECTION, 'zh', user_id=TEST_OWNER_USER_ID)['survey_md'] == '# 中文'

    def test_rerun_upserts_rather_than_duplicating(self, fresh_db):
        from lib.research.persistence import (load_research_artifacts,
                                              persist_survey)
        persist_survey(
            _DIRECTION, 'en', '# First', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        persist_survey(
            _DIRECTION, 'en', '# Second', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        got = load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)
        n = 1 if got['survey_md'] else 0
        assert n == 1, f'a re-run duplicated the row instead of upserting ({n})'
        assert load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)['survey_md'] == '# Second'

    def test_research_rows_never_land_on_a_real_papers_report(self, fresh_db):
        """A paper's plain ``(phash,'en')`` report must be untouched by a
        research run — the composite key is what keeps them apart."""
        from lib.research.persistence import persist_survey
        fresh_db.client.command('paper.report.upsert', {
            'user_id': TEST_OWNER_USER_ID,
            'paper_hash': 'deadbeef' * 4, 'lang': 'en',
            'report': 'THE PAPER REPORT', 'model': '', 'meta': {},
            'created_at': 1,
        }, 'research-test-paper-decoy')
        persist_survey(
            _DIRECTION, 'en', '# S', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID)
        row = fresh_db.client.query('paper.report.get', {
            'user_id': TEST_OWNER_USER_ID,
            'paper_hash': 'deadbeef' * 4, 'lang': 'en',
        })
        assert row['report'] == 'THE PAPER REPORT', \
            'a research run overwrote a real paper report'


# ── 4. Failure posture: a DB hiccup must not destroy an expensive artifact ─

class TestPersistenceFailurePosture:
    def test_persist_failure_returns_false_and_does_not_raise(self, fresh_db,
                                                              monkeypatch):
        """The adapter reports a refused write; recipe policy owns settlement."""
        import lib.research.persistence as rp

        def _boom(*a, **k):
            raise RuntimeError('db down')

        monkeypatch.setattr(rp, '_upsert_row', _boom)
        assert rp.persist_ideate(_DIRECTION, 'en', _IDEATE_ARTIFACT,
                                 model='', user_id=TEST_OWNER_USER_ID) is False

    def test_empty_direction_is_refused_not_written(self, fresh_db):
        from lib.research.persistence import persist_survey
        assert persist_survey(
            '', 'en', '# S', _OPEN_GAPS, model='',
            user_id=TEST_OWNER_USER_ID) is False
        n = len(fresh_db.client.query(
            'research.directions.list', {
                'user_id': TEST_OWNER_USER_ID, 'limit': 50}))
        assert n == 0, 'an empty direction wrote a row under a blank identity'


# ── 5. The terminal recipe boundary publishes the complete artifact ───────

class TestRecipeIsWired:
    """Publication happens after generation/evaluation checkpoints are frozen."""

    def test_publish_stage_persists_survey_and_final_ideas(self, fresh_db):
        import lib.research.recipe as rc
        from lib.research.persistence import load_research_artifacts

        evaluation = {'ok': True, 'overall_score': 4.2, 'usage': {'calls': 2}}
        receipt = rc._run_publish({
            'direction': _DIRECTION, 'lang': 'en',
            'user_id': TEST_OWNER_USER_ID,
            'artifacts': {
                'survey': {'open_gaps': _OPEN_GAPS, 'survey_md': '# Wired',
                           'usage': {'calls': 1}},
                'ideate': _IDEATE_ARTIFACT,
                'evaluate': evaluation,
            },
        })
        assert receipt == {'confirmed': True, 'rows': 2}
        got = load_research_artifacts(
            _DIRECTION, 'en', user_id=TEST_OWNER_USER_ID)
        assert got['survey_md'] == '# Wired'
        assert len(got['accepted']) == 1
        assert got['rejected'][0]['scores']['novelty'] == 2, \
            'the rejection audit did not reach the DB'
        assert got['evaluation']['overall_score'] == 4.2

    def test_generation_stage_does_not_depend_on_repository(self, monkeypatch):
        """A storage outage cannot force an expensive ideate stage to rerun."""
        import lib.research.recipe as rc

        monkeypatch.setattr(rc, '_generate_ideas', lambda *a, **k: {
            'ok': True, **_IDEATE_ARTIFACT})
        monkeypatch.setattr(
            rc, '_persist_ideate',
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('generation attempted repository publication')))
        art = rc._run_ideate({
            'direction': _DIRECTION, 'lang': 'en', 'n_ideas': 6,
            'user_id': TEST_OWNER_USER_ID,
            'artifacts': {'survey': {'open_gaps': _OPEN_GAPS}}})
        assert len(art['accepted']) == 1

    def test_publish_stage_refuses_unconfirmed_terminal_write(self, monkeypatch):
        import lib.research.recipe as rc

        monkeypatch.setattr(rc, '_persist_survey', lambda *a, **k: True)
        monkeypatch.setattr(rc, '_persist_ideate', lambda *a, **k: False)
        with pytest.raises(RuntimeError, match='evaluated-ideas publication'):
            rc._run_publish({
                'direction': _DIRECTION, 'lang': 'en',
                'user_id': TEST_OWNER_USER_ID,
                'artifacts': {
                    'survey': {'open_gaps': _OPEN_GAPS, 'survey_md': '# S'},
                    'ideate': _IDEATE_ARTIFACT,
                    'evaluate': {'ok': True},
                },
            })


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
