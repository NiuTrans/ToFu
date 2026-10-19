"""Continuous-film contracts must diagnose handoffs without inventing evidence."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from lib.motion_video import _continuity as continuity
from lib.motion_video import _render as render


pytestmark = pytest.mark.unit


def plan(kind='transform'):
    handoff = {'kind': kind, 'reason': 'The seed becomes the next focal object.'}
    if kind != 'cut':
        handoff['carrier_id'] = 'seed'
    return {'version': continuity.CONTINUITY_VERSION, 'duration_s': 2,
            'beats': [{'id': 'idea', 'start_s': 0, 'end_s': 1, 'purpose': 'Introduce seed'},
                      {'id': 'system', 'start_s': 1, 'end_s': 2,
                       'purpose': 'Grow seed', 'handoff': handoff}]}


def samples():
    result = []
    for x in (100, 118, 120, 122, 140, 100, 140):
        result.append({'identity': 1, 'visible': True, 'opacity': 1,
                       'x': x, 'y': 100, 'width': 40, 'height': 40,
                       'area_ratio': .02, 'frame_width': 640, 'frame_height': 360,
                       'pixel_digest': f'pixels-{x}'})
    return result


@pytest.fixture
def valid_project(tmp_path):
    (tmp_path / 'continuity.json').write_text(json.dumps(plan()))
    (tmp_path / 'index.html').write_text('<!doctype html><html><body>local fixture</body></html>')
    return tmp_path


def test_valid_contract_and_no_file_leave_ordinary_scenes_unchanged(tmp_path):
    assert continuity.validate_continuity_plan(plan()) == []
    assert continuity.check_continuity_project(str(tmp_path)) == {
        'ok': True, 'status': 'not_requested'}


@pytest.mark.parametrize('mutate,match', [
    (lambda p: p.update(version='future'), 'version'),
    (lambda p: p.update(duration_s=float('nan')), 'duration_s'),
    (lambda p: p.update(duration_s=True), 'duration_s'),
    (lambda p: p.update(duration_s=181), 'duration_s'),
    (lambda p: p.update(duration_s=3), 'exact declared'),
    (lambda p: p.update(beats=p['beats'][:1]), '2..24'),
    (lambda p: p['beats'][1].update(start_s=1.1), 'contiguous'),
    (lambda p: p['beats'][1].update(end_s=1.2), '0.25s'),
    (lambda p: p['beats'][1].update(id='idea'), 'duplicate'),
    (lambda p: p['beats'][1].pop('handoff'), 'explicit incoming'),
    (lambda p: p['beats'][1].update(purpose=''), 'purpose'),
    (lambda p: p['beats'][1]['handoff'].update(carrier_id='#seed'), 'carrier_id'),
    (lambda p: p['beats'][1]['handoff'].update(kind='fade'), 'handoff kind'),
    (lambda p: p['beats'][1]['handoff'].update(kind='cut'), 'must not claim'),
    (lambda p: p['beats'][1]['handoff'].update(reason=''), 'reason'),
    (lambda p: p['beats'][0].update(handoff={'kind': 'cut'}), 'opening'),
])
def test_invalid_contracts_are_actionable(mutate, match):
    value = plan()
    mutate(value)
    assert match in '; '.join(continuity.validate_continuity_plan(value))


def test_invalid_plan_cannot_boot_a_browser(tmp_path, monkeypatch):
    (tmp_path / 'continuity.json').write_text(json.dumps({'version': 'broken'}))
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('worker must not run'))
    result = continuity.check_continuity_project(str(tmp_path))
    assert result['status'] == 'invalid_contract'
    assert result['category'] == 'continuity_failure'


def test_valid_movement_and_explicit_hold_pass():
    assert continuity._boundary_errors(samples(), 'transform') == []
    static = [copy.deepcopy(samples()[0]) for _ in range(7)]
    assert continuity._boundary_errors(static, 'hold') == []
    assert 'no measurable' in '; '.join(continuity._boundary_errors(static, 'transform'))


@pytest.mark.parametrize('field,value,match', [
    ('identity', 2, 'identity'), ('visible', False, 'disappears'),
    ('opacity', 0.05, 'disappears'), ('area_ratio', 0, 'disappears'),
    ('area_ratio', 1, 'background'), ('x', 600, 'jumps'),
    ('height', 200, 'jumps'), ('x', float('nan'), 'non-finite'),
    ('frame_width', 0, 'disappears'),
])
def test_handoff_defects_cannot_pass(field, value, match):
    value_samples = samples()
    value_samples[2][field] = value
    assert match in '; '.join(continuity._boundary_errors(value_samples, 'transform'))


def test_replacement_can_keep_same_pixels_but_not_same_identity():
    value = samples()
    value[5]['identity'] = 10
    assert 'identity' in '; '.join(continuity._boundary_errors(value, 'transform'))


def test_reverse_seek_compares_pixels_not_only_geometry():
    value = samples()
    value[5]['pixel_digest'] = 'changed text at identical geometry'
    assert 'reverse seek' in '; '.join(continuity._boundary_errors(value, 'transform'))


def test_missing_or_incomplete_carriers_fail():
    assert continuity._boundary_errors(samples()[:5], 'hold') == ['incomplete boundary samples']
    value = samples()
    value[2] = {'error': 'carrier missing'}
    assert continuity._boundary_errors(value, 'hold') == ['carrier missing']


def test_all_cuts_are_not_evidence_of_continuity(tmp_path):
    (tmp_path / 'continuity.json').write_text(json.dumps(plan('cut')))
    result = continuity.check_continuity_project(str(tmp_path))
    assert result['ok'] is False
    assert result['checked_boundaries'] == 0
    assert result['declared_cuts'] == 1
    assert result['boundaries'][0]['status'] == 'declared_cut'
    assert 'no carried boundaries' in result['errors'][0]


def test_evidence_directory_cannot_redirect_writes(tmp_path):
    (tmp_path / 'continuity.json').write_text(json.dumps(plan()))
    target = tmp_path / 'outside'
    target.mkdir()
    (tmp_path / '.tofu-continuity').symlink_to(target, target_is_directory=True)
    result = continuity.check_continuity_project(str(tmp_path))
    assert not result['ok']
    assert 'symlink' in result['errors'][0]
    assert list(target.iterdir()) == []


@pytest.mark.parametrize('error,category', [
    (RuntimeError('Chromium missing'), 'infra'),
    (ValueError('timeline QA frame exceeds the finite geometry limit'), 'infra'),
    (TimeoutError('capture deadline exhausted'), 'timeout'),
])
def test_unavailable_evidence_is_not_a_composition_verdict(valid_project, monkeypatch, error, category):
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise error
    monkeypatch.setattr(continuity, '_check_plan', unavailable)
    digest = continuity._source_digest(str(valid_project))
    result = continuity._worker_check(str(valid_project), 10, digest)
    assert len(calls) == 1
    assert result['ok'] is False and result['status'] == 'unavailable'
    assert result['category'] == category
    assert render.is_infra_category(result['category'])
    assert 'source_sha256' not in result
    assert 'contact_sheet' not in result


def test_abort_is_preserved(tmp_path):
    (tmp_path / 'continuity.json').write_text(json.dumps(plan()))
    abort = threading.Event()
    abort.set()
    result = continuity.check_continuity_project(str(tmp_path), abort_event=abort)
    assert result['category'] == 'aborted'
    assert result['ok'] is False and result['status'] == 'aborted'


def test_check_adds_continuity_failure_to_aggregate(tmp_path, monkeypatch):
    monkeypatch.setattr(render, '_gate', lambda *a, **kw: {'ok': True})
    (tmp_path / 'continuity.json').write_text('{}')
    result = render.check_project(str(tmp_path))
    assert result['ok'] is False
    assert result['category'] == 'continuity_failure'
    assert result['errors']
    assert result['gates']['continuity']['status'] == 'invalid_contract'


def test_static_failure_does_not_try_another_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(render, '_gate', lambda *a, **kw: {'ok': False, 'category': 'lint'})
    monkeypatch.setattr(continuity, 'check_continuity_project', lambda *a, **kw: pytest.fail('extra browser'))
    assert render.check_project(str(tmp_path))['category'] == 'lint'


def test_render_refuses_invalid_contract_even_without_explicit_check(tmp_path, monkeypatch):
    monkeypatch.setattr(render, '_cli_or_env_error', lambda: '/fake/hyperframes')
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('must not render'))
    (tmp_path / 'continuity.json').write_text('{}')
    output = tmp_path / 'film.mp4'
    output.write_bytes(b'previous published video')
    result = render.render_project(str(tmp_path), str(output))
    assert result['category'] == 'continuity_failure'
    assert result['output'] == ''
    assert output.read_bytes() == b'previous published video'


def test_declared_failure_category_is_in_single_partition():
    assert 'continuity_failure' in render.COMPOSITION_CATEGORIES
    assert not render.is_infra_category('continuity_failure')


@pytest.mark.parametrize('field', ['duration_s', 'start_s', 'end_s'])
def test_huge_json_numbers_are_invalid_contracts_before_worker_start(tmp_path, monkeypatch, field):
    value = plan()
    target = value if field == 'duration_s' else value['beats'][1]
    target[field] = 10 ** 400
    assert continuity.validate_continuity_plan(value)
    (tmp_path / 'continuity.json').write_text(json.dumps(value))
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('invalid contract launched worker'))
    result = continuity.check_continuity_project(str(tmp_path))
    assert result['ok'] is False
    assert result['status'] == 'invalid_contract'
    assert result['category'] == 'continuity_failure'
    assert result['errors']


@pytest.mark.parametrize('payload_kind', ['integer_digits', 'recursive_json'])
def test_json_decoder_limits_return_typed_contract_failure(tmp_path, monkeypatch, payload_kind):
    if payload_kind == 'integer_digits':
        raw = json.dumps(plan()).replace('"duration_s": 2', '"duration_s": ' + '9' * 5000)
    else:
        depth = sys.getrecursionlimit() + 100
        raw = json.dumps(plan())[:-1] + ', "nested": ' + '[' * depth + '0' + ']' * depth + '}'
    assert len(raw) < continuity.MAX_PLAN_BYTES
    (tmp_path / 'continuity.json').write_text(raw)
    if payload_kind == 'recursive_json':
        # Decoder backends do not share Python's nesting limit. Exercise the
        # decoder's typed recursion failure without banning unused metadata.
        def recursive_decode(raw):
            raise RecursionError('nested JSON exceeds the decoder recursion limit')
        monkeypatch.setattr(continuity.json, 'loads', recursive_decode)
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('invalid JSON launched worker'))
    result = continuity.check_continuity_project(str(tmp_path))
    assert result['ok'] is False
    assert result['status'] == 'invalid_contract'
    assert result['category'] == 'continuity_failure'
    assert result['errors']


@pytest.mark.timeout(10)
@pytest.mark.parametrize('filename', ['continuity.json', 'index.html'])
@pytest.mark.parametrize('kind', ['fifo', 'directory', 'symlink'])
def test_nonregular_sources_fail_promptly_without_worker(valid_project, monkeypatch, filename, kind):
    source = valid_project / filename
    original = source.read_bytes()
    source.unlink()
    if kind == 'fifo':
        os.mkfifo(source)
    elif kind == 'directory':
        source.mkdir()
    else:
        target = valid_project / 'regular-target'
        target.write_bytes(original)
        source.symlink_to(target)
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('nonregular input launched worker'))
    start = time.monotonic()
    result = continuity.check_continuity_project(str(valid_project), timeout=1)
    assert time.monotonic() - start < 3, result
    assert result['ok'] is False
    if filename == 'continuity.json' and kind in ('fifo', 'directory'):
        assert result['status'] == 'invalid_contract'
        assert result['category'] == 'continuity_failure'
    else:
        assert result['status'] == 'unavailable'
        assert render.is_infra_category(result['category'])
    assert filename in '; '.join(result['errors'])


@pytest.mark.parametrize('swap_at', ['scratch_open', 'publish'])
def test_directory_replaced_by_symlink_during_publication_cannot_escape(tmp_path, monkeypatch, swap_at):
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'boundary-001.png'
    victim.write_bytes(b'outside evidence must survive')
    moved = tmp_path / 'pinned-evidence'
    swaps = []
    with continuity._evidence_directory(str(tmp_path)) as (directory, fd):
        previous = directory / 'boundary-001.png'
        previous.write_bytes(b'previous evidence')
        operation = 'open' if swap_at == 'scratch_open' else 'replace'
        original = getattr(os, operation)

        def swap_directory_then_operate(*args, **kwargs):
            directory.rename(moved)
            directory.symlink_to(outside, target_is_directory=True)
            swaps.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(continuity.os, operation, swap_directory_then_operate)
        with pytest.raises(RuntimeError, match='directory changed'):
            continuity._publish_preview(directory, fd, 'boundary-001.png', b'new preview')
    assert swaps == [True]
    assert victim.read_bytes() == b'outside evidence must survive'
    assert sorted(path.name for path in outside.iterdir()) == ['boundary-001.png']
    assert not list(moved.glob('*.pending'))


@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_existing_pending_link_cannot_truncate_an_outside_target(tmp_path, link_kind):
    victim = tmp_path / 'outside.txt'
    victim.write_bytes(b'outside data must never be truncated')
    with continuity._evidence_directory(str(tmp_path)) as (directory, fd):
        pending = directory / '.boundary-001.png.pending'
        if link_kind == 'symlink':
            pending.symlink_to(victim)
        else:
            os.link(victim, pending)
        output = continuity._publish_preview(directory, fd, 'boundary-001.png', b'new preview')
        assert output == str(directory / 'boundary-001.png')
        assert (directory / 'boundary-001.png').read_bytes() == b'new preview'
        assert not os.path.lexists(pending)
    assert victim.read_bytes() == b'outside data must never be truncated'


@pytest.mark.parametrize('ok', [True, False])
def test_worker_attaches_source_digest_to_completed_composition_verdict(valid_project, monkeypatch, ok):
    evidence = {'ok': ok, 'status': 'sampled_pass' if ok else 'failed',
                'errors': [] if ok else ['carrier disappears'], 'checked_boundaries': 1}
    monkeypatch.setattr(continuity, '_check_plan', lambda *args: dict(evidence))
    digest = continuity._source_digest(str(valid_project))
    result = continuity._worker_check(str(valid_project), 10, digest)
    assert result['ok'] is ok
    assert result['status'] == evidence['status']
    assert result['errors'] == evidence['errors']
    assert result['source_sha256'] == digest
    if not ok:
        assert result['category'] == 'continuity_failure'
        assert not render.is_infra_category(result['category'])


@pytest.mark.parametrize('filename', ['index.html', 'continuity.json'])
@pytest.mark.parametrize('change_at', ['before', 'during'])
def test_worker_rejects_changed_sources_instead_of_returning_stale_verdict(
        valid_project, monkeypatch, filename, change_at):
    project = str(valid_project)
    digest = continuity._source_digest(project)
    source = valid_project / filename
    changed = source.read_bytes() + b'\n'
    calls = []
    def capture(*args):
        calls.append(args)
        source.write_bytes(changed)
        return {'ok': True, 'status': 'sampled_pass', 'errors': []}
    monkeypatch.setattr(continuity, '_check_plan', capture)
    if change_at == 'before':
        source.write_bytes(changed)
    result = continuity._worker_check(project, 10, digest)
    assert len(calls) == (0 if change_at == 'before' else 1)
    assert result['ok'] is False and result['status'] == 'unavailable'
    assert result['category'] == 'infra'
    assert 'changed' in '; '.join(result['errors'])
    assert 'source_sha256' not in result
    assert 'boundaries' not in result


@pytest.mark.timeout(15)
def test_worker_receipt_larger_than_6000_chars_survives_real_cli_stdout(valid_project, monkeypatch):
    # Exercise the existing subprocess/stdout owner, substituting only capture
    # with a finite local emitter. A diagnostic tail is not a worker protocol.
    digest = continuity._source_digest(str(valid_project))
    receipt = {'ok': True, 'status': 'sampled_pass', 'source_sha256': digest,
               'errors': [], 'boundaries': [
                   {'beat_id': f'beat-{index}', 'status': 'sampled_pass',
                    'reason': 'The same seed carries the focal idea into the next beat. ' * 6}
                   for index in range(1, 24)]}
    encoded = json.dumps(receipt)
    assert len(encoded) > 6000
    run_cli = render._run_cli
    runs = []
    def emit_receipt(args, *, cwd, timeout, abort_event=None):
        script = ('import sys; print("worker startup diagnostic"); '
                  'print("TOFU_CONTINUITY_RESULT=" + sys.argv[1]); '
                  'print("worker shutdown diagnostic")')
        result = run_cli([sys.executable, '-c', script, encoded], cwd=str(valid_project),
                         timeout=timeout, abort_event=abort_event, env=dict(os.environ))
        runs.append(result)
        return result
    monkeypatch.setattr(render, '_run_cli', emit_receipt)
    result = continuity.check_continuity_project(str(valid_project), timeout=5)
    assert len(runs) == 1 and len(runs[0]['out']) > 6000
    assert runs[0]['rc'] == 0 and not runs[0]['category']
    assert result == receipt


@pytest.mark.parametrize('receipt', [
    'not JSON', '{"ok": "yes"}',
    '{"ok": true, "source_sha256": "different source"}',
])
def test_invalid_or_mismatched_worker_receipt_is_unavailable(valid_project, monkeypatch, receipt):
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: {
        'rc': 0, 'out': 'TOFU_CONTINUITY_RESULT=' + receipt,
        'err': '', 'category': '', 'elapsed': 0})
    result = continuity.check_continuity_project(str(valid_project))
    assert result['ok'] is False and result['status'] == 'unavailable'
    assert result['category'] == 'infra'
    assert result['errors']
    assert 'boundaries' not in result


@pytest.mark.parametrize('category', ['aborted', 'timeout'])
def test_stopped_worker_preserves_status_and_preview_and_cleans_pending(valid_project, monkeypatch, category):
    directory = valid_project / '.tofu-continuity'
    directory.mkdir()
    previous = directory / 'boundary-001.png'
    previous.write_bytes(b'previous complete preview')
    unrelated = directory / '.unrelated.pending'
    unrelated.write_bytes(b'not owned by the continuity worker')
    pending = [directory / f'.boundary-{index:03d}.png.pending' for index in (1, 23)]
    abort = threading.Event()
    calls = []
    def stopped_worker(args, *, cwd, timeout, abort_event=None):
        assert abort_event is abort
        calls.append(args)
        for path in pending:
            path.write_bytes(b'interrupted publication')
        if category == 'aborted':
            abort.set()
        return {'rc': -9, 'out': '', 'err': '', 'category': category, 'elapsed': 0}
    monkeypatch.setattr(render, '_run_cli', stopped_worker)
    result = continuity.check_continuity_project(str(valid_project), abort_event=abort)
    assert len(calls) == 1
    assert result['ok'] is False
    assert result['category'] == category
    assert result['status'] == ('aborted' if category == 'aborted' else 'unavailable')
    assert 'boundaries' not in result and 'contact_sheet' not in result
    assert previous.read_bytes() == b'previous complete preview'
    assert all(not os.path.lexists(path) for path in pending)
    assert unrelated.read_bytes() == b'not owned by the continuity worker'


@pytest.mark.timeout(10)
def test_stopped_worker_cleanup_cannot_delete_another_active_captures_pending(valid_project, monkeypatch):
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: {
        'rc': -9, 'out': '', 'err': '', 'category': 'timeout', 'elapsed': 0})
    with continuity._evidence_directory(str(valid_project)) as (directory, fd):
        pending = directory / '.boundary-001.png.pending'
        pending.write_bytes(b'owned by the still-active capture')
        start = time.monotonic()
        result = continuity.check_continuity_project(str(valid_project), timeout=1)
        assert time.monotonic() - start < 3
        assert result['ok'] is False and result['category'] == 'timeout'
        assert pending.read_bytes() == b'owned by the still-active capture'
    assert not pending.exists()


@pytest.fixture
def render_receipt(valid_project, monkeypatch):
    receipt = {'ok': True, 'status': 'sampled_pass', 'errors': [],
               'source_sha256': continuity._source_digest(str(valid_project)),
               'checked_boundaries': 1}
    monkeypatch.setattr(continuity, 'check_continuity_project',
                        lambda *a, **kw: dict(receipt))
    monkeypatch.setattr(render, '_cli_or_env_error', lambda: '/fake/hyperframes')
    return receipt


@pytest.mark.parametrize('filename', ['index.html', 'continuity.json'])
@pytest.mark.parametrize('change_at', ['before', 'after'])
def test_render_fences_receipt_before_and_after_cli(
        valid_project, render_receipt, monkeypatch, filename, change_at):
    output_dir = valid_project / 'outputs'
    output_dir.mkdir()
    output = output_dir / 'film.mp4'
    output.write_bytes(b'previous published video')
    source = valid_project / filename
    changed = source.read_bytes() + b'\n'
    calls = []

    def run(args, **kwargs):
        staged = Path(args[args.index('-o') + 1])
        calls.append(staged)
        staged.write_bytes(b'rendered from changed source')
        if change_at == 'after':
            source.write_bytes(changed)
        return {'rc': 0, 'out': 'rendered in 1.5s', 'err': '', 'category': '', 'elapsed': 2}

    monkeypatch.setattr(render, '_run_cli', run)
    if change_at == 'before':
        source.write_bytes(changed)
    result = render.render_project(str(valid_project), str(output))
    assert result['ok'] is False and result['category'] == 'infra'
    assert result['output'] == ''
    assert result['continuity']['status'] == 'unavailable'
    assert 'changed' in result['detail']
    assert 'source_sha256' not in result['continuity']
    assert len(calls) == (0 if change_at == 'before' else 1)
    assert output.read_bytes() == b'previous published video'
    assert list(output_dir.iterdir()) == [output]
    assert all(staged != output and not staged.parent.exists() for staged in calls)


@pytest.mark.parametrize('relative_output', [False, True])
@pytest.mark.parametrize('relative_project', [False, True])
def test_render_unchanged_receipt_publishes_from_unique_private_stage(
        valid_project, render_receipt, monkeypatch, relative_output, relative_project):
    output_dir = valid_project / 'exports'
    output_dir.mkdir()
    output = output_dir / 'film.mp4'
    output.write_bytes(b'previous published video')
    elsewhere = valid_project / 'unrelated-cwd'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    requested = 'exports/film.mp4' if relative_output else str(output)
    project_arg = os.path.relpath(valid_project, elsewhere) if relative_project else str(valid_project)
    staged_paths = []
    captures = []

    def capture(*args, **kwargs):
        captures.append(args)
        return dict(render_receipt)

    def run(args, *, cwd, timeout, abort_event):
        staged = Path(args[args.index('-o') + 1])
        assert cwd == str(valid_project)
        assert args[-2:] == ['--fps', '24']
        assert 0 < timeout <= 20
        assert staged.is_absolute() and staged.parent.parent == output_dir
        assert staged.parent.stat().st_mode & 0o777 == 0o700
        assert staged.parent.stat().st_dev == output_dir.stat().st_dev
        assert output.read_bytes() == (b'new video' if staged_paths else b'previous published video')
        staged_paths.append(staged)
        staged.write_bytes(b'new video')
        return {'rc': 0, 'out': 'rendered in 1.5s', 'err': '', 'category': '', 'elapsed': 2}

    monkeypatch.setattr(continuity, 'check_continuity_project', capture)
    monkeypatch.setattr(render, '_run_cli', run)
    for _ in range(2):
        result = render.render_project(project_arg, requested, fps=24, timeout=20)
        assert result == {'ok': True, 'category': '', 'output': requested,
                          'elapsed': 2, 'render_time_s': 1.5, 'detail': '',
                          'continuity': render_receipt}
        assert output.read_bytes() == b'new video'
        assert list(output_dir.iterdir()) == [output]
    assert len(captures) == 2 and len(set(staged_paths)) == 2
    assert all(not path.parent.exists() for path in staged_paths)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize('outcome', ['failure', 'aborted', 'timeout', 'missing', 'empty', 'late_abort'])
def test_render_unsuccessful_cli_preserves_old_output_and_reclaims_stage(
        valid_project, render_receipt, monkeypatch, outcome):
    output = valid_project / 'film.mp4'
    output.write_bytes(b'previous published video')
    before = set(valid_project.iterdir())
    abort = threading.Event()
    staged_paths = []

    def run(args, **kwargs):
        staged = Path(args[args.index('-o') + 1])
        staged_paths.append(staged)
        if outcome != 'missing':
            staged.write_bytes(b'' if outcome == 'empty' else b'partial video')
        if outcome == 'late_abort':
            abort.set()
        return {'rc': 1 if outcome == 'failure' else 0, 'out': '',
                'err': 'render interrupted', 'elapsed': 1,
                'category': outcome if outcome in ('aborted', 'timeout') else ''}

    monkeypatch.setattr(render, '_run_cli', run)
    result = render.render_project(str(valid_project), str(output), abort_event=abort)
    expected = 'aborted' if outcome == 'late_abort' else outcome
    assert result['ok'] is False and result['output'] == ''
    assert result['category'] == (expected if expected in ('aborted', 'timeout') else 'unknown')
    assert output.read_bytes() == b'previous published video'
    assert len(staged_paths) == 1 and staged_paths[0] != output
    assert not staged_paths[0].parent.exists()
    assert set(valid_project.iterdir()) == before


def test_render_cancelled_after_capture_does_not_start_cli(valid_project, render_receipt, monkeypatch):
    abort = threading.Event()
    abort.set()
    output = valid_project / 'film.mp4'
    output.write_bytes(b'previous published video')
    before = set(valid_project.iterdir())
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('cancelled render launched'))
    result = render.render_project(str(valid_project), str(output), abort_event=abort)
    assert result['ok'] is False and result['category'] == 'aborted'
    assert result['output'] == ''
    assert output.read_bytes() == b'previous published video'
    assert set(valid_project.iterdir()) == before


@pytest.mark.parametrize('primitive', ['O_NONBLOCK', 'O_NOFOLLOW', 'O_DIRECTORY', 'fcntl', 'flock', 'dir_fd'])
def test_unsupported_os_is_unavailable_only_for_opted_in_films(valid_project, monkeypatch, primitive):
    ordinary = valid_project / 'ordinary'
    ordinary.mkdir()
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('unsupported host launched worker'))
    with monkeypatch.context() as patch:
        if primitive == 'fcntl':
            patch.setitem(sys.modules, 'fcntl', None)
        elif primitive == 'flock':
            import fcntl
            patch.delattr(fcntl, 'flock')
        elif primitive == 'dir_fd':
            patch.setattr(os, 'supports_dir_fd', set())
        else:
            patch.delattr(os, primitive)
        result = continuity.check_continuity_project(str(valid_project))
        assert continuity.check_continuity_project(str(ordinary)) == {
            'ok': True, 'status': 'not_requested'}
    assert result['ok'] is False and result['status'] == 'unavailable'
    assert result['category'] == 'infra'
    assert result['errors']


@pytest.mark.parametrize('error', [AttributeError('missing primitive'),
                                  NotImplementedError('dir_fd unavailable'),
                                  ImportError('fcntl unavailable')])
def test_unsupported_os_during_worker_cleanup_returns_unavailable(valid_project, monkeypatch, error):
    digest = continuity._source_digest(str(valid_project))
    receipt = {'ok': True, 'status': 'sampled_pass', 'source_sha256': digest, 'errors': []}
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: {
        'rc': 0, 'out': 'TOFU_CONTINUITY_RESULT=' + json.dumps(receipt),
        'err': '', 'category': '', 'elapsed': 0})

    def unsupported(*args):
        raise error

    monkeypatch.setattr(continuity, '_evidence_directory', unsupported)
    result = continuity.check_continuity_project(str(valid_project))
    assert result['ok'] is False and result['status'] == 'unavailable'
    assert result['category'] == 'infra'
    assert result['errors']


def test_ordinary_render_keeps_direct_output_and_exact_result_shape(tmp_path, monkeypatch):
    output = tmp_path / 'ordinary.mp4'
    monkeypatch.setattr(render, '_cli_or_env_error', lambda: '/fake/hyperframes')

    def run(args, **kwargs):
        assert args[args.index('-o') + 1] == str(output)
        output.write_bytes(b'ordinary render')
        return {'rc': 0, 'out': 'rendered in 1.5s', 'err': '', 'category': '', 'elapsed': 2}

    monkeypatch.setattr(render, '_run_cli', run)
    with monkeypatch.context() as patch:
        patch.delattr(os, 'O_NOFOLLOW')
        result = render.render_project(str(tmp_path), str(output))
    assert result == {'ok': True, 'category': '', 'output': str(output),
                      'elapsed': 2, 'render_time_s': 1.5, 'detail': ''}
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize('filename', ['index.html', 'continuity.json'])
@pytest.mark.parametrize('change', ['removed', 'symlink'])
def test_render_does_not_publish_when_checked_source_disappears_or_becomes_link(
        valid_project, render_receipt, monkeypatch, filename, change):
    output_dir = valid_project / 'exports'
    output_dir.mkdir()
    output = output_dir / 'film.mp4'
    output.write_bytes(b'previous video')
    source = valid_project / filename
    unchanged_source = source.read_bytes()
    link_target = valid_project / 'outside-source'
    link_target.write_bytes(unchanged_source)

    def run(args, **kwargs):
        Path(args[args.index('-o') + 1]).write_bytes(b'new video')
        source.unlink()
        if change == 'symlink':
            source.symlink_to(link_target)
        return {'rc': 0, 'out': '', 'err': '', 'category': '', 'elapsed': 1}

    monkeypatch.setattr(render, '_run_cli', run)
    result = render.render_project(str(valid_project), str(output))
    assert result['ok'] is False and result['category'] == 'infra'
    assert result['continuity']['status'] == 'unavailable'
    assert result['output'] == ''
    assert output.read_bytes() == b'previous video'
    assert link_target.read_bytes() == unchanged_source
    assert list(output_dir.iterdir()) == [output]


def test_render_requires_a_source_bound_receipt(valid_project, render_receipt, monkeypatch):
    render_receipt.pop('source_sha256')
    output = valid_project / 'film.mp4'
    before = set(valid_project.iterdir())
    monkeypatch.setattr(render, '_run_cli', lambda *a, **kw: pytest.fail('unbound receipt rendered'))
    result = render.render_project(str(valid_project), str(output))
    assert result['ok'] is False and result['category'] == 'infra'
    assert result['continuity']['status'] == 'unavailable'
    assert result['output'] == '' and not output.exists()
    assert set(valid_project.iterdir()) == before


@pytest.mark.parametrize('failure_at', ['staging', 'cli', 'replace'])
def test_render_output_io_failure_preserves_published_file_and_reclaims_stage(
        valid_project, render_receipt, monkeypatch, failure_at):
    output = valid_project / 'film.mp4'
    output.write_bytes(b'previous video')
    before = set(valid_project.iterdir())
    staged_paths = []

    def unavailable(*args, **kwargs):
        raise OSError('injected output I/O failure')

    def run(args, **kwargs):
        staged = Path(args[args.index('-o') + 1])
        staged_paths.append(staged)
        staged.write_bytes(b'new video')
        if failure_at == 'cli':
            unavailable()
        return {'rc': 0, 'out': '', 'err': '', 'category': '', 'elapsed': 1}

    monkeypatch.setattr(render, '_run_cli', run)
    if failure_at == 'staging':
        monkeypatch.setattr(render.tempfile, 'TemporaryDirectory', unavailable)
    elif failure_at == 'replace':
        monkeypatch.setattr(render.os, 'replace', unavailable)
    result = render.render_project(str(valid_project), str(output))
    assert result['ok'] is False and result['category'] == 'io'
    assert result['output'] == '' and 'injected output I/O failure' in result['detail']
    assert output.read_bytes() == b'previous video'
    assert set(valid_project.iterdir()) == before
    assert len(staged_paths) == (0 if failure_at == 'staging' else 1)
    assert all(path != output and not path.parent.exists() for path in staged_paths)
