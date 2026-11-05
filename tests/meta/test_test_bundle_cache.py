"""Cross-process test-bundle cache in tests/_runtime_sections.py.

The cache must reuse a bundle only while every input the bundler read is
byte-identical, and must never persist output tied to one session's view dir.
"""
import json
from pathlib import Path

import pytest

from tests import _runtime_sections as rs

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_bundler(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', str(cache))
    monkeypatch.setattr(rs, '_bundle_environment_digest', lambda: 'env-1')
    monkeypatch.setattr(rs, '_input_digest_memo', {})
    # Files created by the test are seconds old; pretend they have settled.
    monkeypatch.setattr(rs, '_bundle_clock_ns',
                        lambda: rs.time.time_ns() + 10_000_000_000)
    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src' / 'owner.ts'
    source.write_text('export const a = 1;\n', encoding='utf-8')
    calls = []
    state = {'body': 'bundle-v1'}

    def run(argv, **_kwargs):
        calls.append(argv)
        manifest = next(a.split('=', 1)[1] for a in argv
                        if a.startswith('--inputs-manifest='))
        outfile = next(a.split('=', 1)[1] for a in argv
                       if a.startswith('--outfile='))
        Path(outfile).parent.mkdir(parents=True, exist_ok=True)
        Path(outfile).write_text(state['body'], encoding='utf-8')
        Path(manifest).write_text(json.dumps([str(source)]), encoding='utf-8')

        class Result:
            returncode = 0
            stderr = ''
        return Result()

    monkeypatch.setattr(rs.subprocess, 'run', run)
    out = tmp_path / 'view' / 'owner.js'

    def bundle():
        rs._run_test_bundler([str(source), f'--outfile={out}'], [out],
                             timeout=5, failure='bundle failed')
        return out.read_text(encoding='utf-8')

    return bundle, calls, source, state, out


def test_identical_inputs_reuse_the_cached_bundle(fake_bundler):
    bundle, calls, _source, _state, out = fake_bundler
    assert bundle() == 'bundle-v1'
    out.unlink()
    assert bundle() == 'bundle-v1'
    assert len(calls) == 1


def test_changed_recorded_input_rebuilds(fake_bundler):
    bundle, calls, source, state, _out = fake_bundler
    bundle()
    source.write_text('export const a = 22;\n', encoding='utf-8')
    state['body'] = 'bundle-v2'
    assert bundle() == 'bundle-v2'
    assert len(calls) == 2


def test_deleted_recorded_input_rebuilds(fake_bundler):
    bundle, calls, source, _state, _out = fake_bundler
    bundle()
    source.unlink()
    bundle()
    assert len(calls) == 2


def test_new_file_beside_an_input_rebuilds(fake_bundler):
    """A new sibling can change module resolution without touching any
    recorded input; the input directory listing must invalidate the record."""
    bundle, calls, source, _state, _out = fake_bundler
    bundle()
    (source.parent / 'owner').mkdir()
    bundle()
    assert len(calls) == 2


def test_input_modified_inside_the_racy_window_is_never_recorded(
        fake_bundler, monkeypatch):
    """Whole-second mtimes cannot prove a just-written input was the version
    the build read, so such a build must not be cached."""
    bundle, calls, _source, _state, _out = fake_bundler
    monkeypatch.setattr(rs, '_bundle_clock_ns', rs.time.time_ns)
    bundle()
    bundle()
    assert len(calls) == 2


def test_digest_of_a_recently_modified_file_is_not_memoized(tmp_path, monkeypatch):
    """A same-second, same-size rewrite keeps the stat signature on a coarse
    mount; memoizing a fresh file's digest would hide that rewrite."""
    monkeypatch.setattr(rs, '_input_digest_memo', {})
    path = tmp_path / 'owner.ts'
    path.write_text('export const a = 1;\n', encoding='utf-8')
    assert rs._memoized_digest(str(path)) is not None
    assert rs._input_digest_memo == {}


def test_environment_change_rebuilds(fake_bundler, monkeypatch):
    bundle, calls, _source, _state, _out = fake_bundler
    bundle()
    monkeypatch.setattr(rs, '_bundle_environment_digest', lambda: 'env-2')
    bundle()
    assert len(calls) == 2


def test_output_naming_the_session_view_dir_is_not_cached(fake_bundler):
    bundle, calls, _source, state, _out = fake_bundler
    state['body'] = f'require({str(rs._DIRECTORY)!r})'
    bundle()
    bundle()
    assert len(calls) == 2


def test_cache_can_be_disabled(fake_bundler, monkeypatch):
    bundle, calls, _source, _state, _out = fake_bundler
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', 'off')
    bundle()
    bundle()
    assert len(calls) == 2
