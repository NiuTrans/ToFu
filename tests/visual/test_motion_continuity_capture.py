"""Real-browser acceptance of the continuous-film handoff contract (no model/network)."""

import io
import json
import os
import signal
import threading
import time

import psutil
import pytest
from PIL import Image

from lib.design_sys.temporal_qa import screenshot_timeline_contact_sheet
from lib.motion_video import _render as render
from lib.motion_video._continuity import CONTINUITY_VERSION, check_continuity_project

pytestmark = pytest.mark.visual


def composition(tmp_path, defect=''):
    script = """
      let seeks = 0;
      window.__timelines = { main: {
        pause() { return this; },
        totalTime(seconds) {
          seeks++;
          let seed = document.getElementById('seed');
          DEFECT
          seed.style.transform = `translate(${20 + seconds * 40}px, 70px)`;
          return this;
        }
      }};
    """
    defects = {
        '': '',
        'replacement': "seed.replaceWith(seed.cloneNode(true)); seed = document.getElementById('seed');",
        'hidden': "seed.style.opacity = seconds >= 1 ? '0' : '1';",
        'ancestor_hidden': "seed.parentElement.style.opacity = '0';",
        'missing': "seed.id = 'missing';",
        'duplicate': "const extra = seed.cloneNode(true); seed.parentElement.append(extra);",
        'teleport': "seconds += seconds >= 1 ? 5 : 0;",
        'nondeterministic': "document.body.style.background = seeks % 2 ? '#eeeeee' : '#dddddd';",
        'hung_seek': 'while (true) {}',
    }
    script = script.replace('DEFECT', defects[defect])
    (tmp_path / 'index.html').write_text(
        '<!doctype html><html><body style="margin:0;background:#eee">'
        '<div data-composition-id="main" data-duration="2" '
        'data-width="320" data-height="180" style="position:relative;width:320px;height:180px">'
        '<div id="seed" style="position:absolute;left:0;top:0;width:40px;height:40px;'
        'background:#087c6d;border-radius:10px"></div></div>'
        f'<script>{script}</script></body></html>')
    (tmp_path / 'continuity.json').write_text(json.dumps({
        'version': CONTINUITY_VERSION, 'duration_s': 2,
        'beats': [{'id': 'seed-beat', 'start_s': 0, 'end_s': 1, 'purpose': 'Introduce seed'},
                  {'id': 'growth-beat', 'start_s': 1, 'end_s': 2, 'purpose': 'Grow seed',
                   'handoff': {'kind': 'transform', 'carrier_id': 'seed',
                               'reason': 'Same seed travels to its new role'}}]}))


def test_persistent_carrier_and_reverse_seeking_produce_boundary_evidence(tmp_path):
    composition(tmp_path)
    result = check_continuity_project(str(tmp_path))
    assert result['ok'] is True, result
    assert result['status'] == 'sampled_pass'
    assert result['checked_boundaries'] == 1
    boundary = result['boundaries'][0]
    assert boundary['sample_times_s'] == pytest.approx([.8, 1 - 1/60, 1, 1 + 1/60, 1.2, .8, 1.2])
    with Image.open(boundary['contact_sheet']) as image:
        assert image.width == 7 * 320 + 6 * 12
        assert image.height == 180 + 42


@pytest.mark.parametrize('defect,match', [
    ('replacement', 'identity'), ('hidden', 'disappears'),
    ('ancestor_hidden', 'disappears'), ('teleport', 'jumps'),
    ('nondeterministic', 'reverse seek'), ('duplicate', 'exactly one'),
])
def test_real_boundary_defects_are_not_visual_passes(tmp_path, defect, match):
    composition(tmp_path, defect)
    result = check_continuity_project(str(tmp_path))
    assert result['ok'] is False, result
    assert result['status'] == 'failed', result
    assert result['category'] == 'continuity_failure'
    assert match in '; '.join(result['errors']), result


def test_failed_timeline_does_not_claim_stale_preview(tmp_path):
    composition(tmp_path, 'missing')
    previews = tmp_path / '.tofu-continuity'
    previews.mkdir()
    previous = previews / 'boundary-001.png'
    previous.write_bytes(b'old preview')
    result = check_continuity_project(str(tmp_path))
    assert result['ok'] is False
    assert result['status'] == 'unavailable'
    assert 'contact_sheet' not in result
    assert previous.read_bytes() == b'old preview'


def test_observer_failure_preserves_previous_sheet_and_propagates(tmp_path):
    composition(tmp_path)
    output = tmp_path / 'sheet.png'
    output.write_bytes(b'old evidence')
    def observe(*args):
        raise RuntimeError('observer failed')
    with pytest.raises(RuntimeError, match='observer failed'):
        screenshot_timeline_contact_sheet(str(tmp_path), str(output), sample_observer=observe)
    assert output.read_bytes() == b'old evidence'


def test_observer_cancellation_cannot_publish_partial_evidence(tmp_path):
    composition(tmp_path)
    abort = threading.Event()
    output = tmp_path / 'sheet.png'
    output.write_bytes(b'old evidence')
    seen = []
    def observe(page, seconds, raw):
        seen.append(seconds)
        assert raw.startswith(b'\x89PNG')
        abort.set()
    with pytest.raises(InterruptedError):
        screenshot_timeline_contact_sheet(str(tmp_path), str(output),
                                         sample_observer=observe, abort_check=abort.is_set)
    assert len(seen) == 1
    assert output.read_bytes() == b'old evidence'


@pytest.mark.timeout(30)
def test_in_memory_capture_returns_png_without_publishing_a_file(tmp_path):
    composition(tmp_path)
    raw = screenshot_timeline_contact_sheet(str(tmp_path), None,
                                           progresses=[.4, .6], settle_ms=0)
    assert isinstance(raw, bytes) and raw.startswith(b'\x89PNG\r\n\x1a\n')
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (2 * 320 + 12, 180 + 42)
    assert sorted(path.name for path in tmp_path.iterdir()) == ['continuity.json', 'index.html']


@pytest.mark.timeout(60)
def test_infinite_synchronous_seek_is_killed_without_replacing_preview(tmp_path, monkeypatch):
    composition(tmp_path, 'hung_seek')
    directory = tmp_path / '.tofu-continuity'
    directory.mkdir()
    previous = directory / 'boundary-001.png'
    previous.write_bytes(b'previous complete evidence')
    (directory / '.boundary-001.png.pending').write_bytes(b'stale interrupted publication')
    workers = []
    descendants = []
    descendant_names = []
    popen = render.subprocess.Popen
    killpg = os.killpg

    def record_worker(args, *positional, **kwargs):
        process = popen(args, *positional, **kwargs)
        if 'lib.motion_video._continuity' in args:
            workers.append(process)
        return process

    def record_descendants_before_kill(pgid, sig):
        if any(worker.pid == pgid for worker in workers):
            children = psutil.Process(pgid).children(recursive=True)
            descendants.extend(children)
            for child in children:
                try:
                    descendant_names.append(child.name().lower())
                except psutil.NoSuchProcess:
                    pass
        return killpg(pgid, sig)

    def alive(process):
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    monkeypatch.setattr(render.subprocess, 'Popen', record_worker)
    monkeypatch.setattr(render.os, 'killpg', record_descendants_before_kill)
    try:
        start = time.monotonic()
        result = check_continuity_project(str(tmp_path), timeout=8)
        elapsed = time.monotonic() - start
        assert elapsed < 30, (elapsed, result)
        assert result['ok'] is False and result['status'] == 'unavailable', result
        assert result['category'] == 'timeout', result
        assert 'boundaries' not in result and 'contact_sheet' not in result
        assert workers and all(worker.poll() == -signal.SIGKILL for worker in workers)
        assert any('chrome' in name or 'chromium' in name for name in descendant_names), descendant_names
        deadline = time.monotonic() + 5
        while any(alive(child) for child in descendants) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not [child.pid for child in descendants if alive(child)], 'capture left live descendants'
        assert previous.read_bytes() == b'previous complete evidence'
        assert sorted(path.name for path in directory.iterdir()) == ['boundary-001.png']
    finally:
        # The regression must not strand its own renderer even if teardown is
        # broken. Only identities descended from this test's worker are owned.
        for worker in workers:
            if worker.poll() is None:
                try:
                    descendants.extend(psutil.Process(worker.pid).children(recursive=True))
                    killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for child in descendants:
            try:
                if alive(child):
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        for worker in workers:
            worker.wait(timeout=5)
