"""Opt-in continuous-film contracts and sampled handoff evidence.

Entry point: ``check_continuity_project``; invoked by the existing check/render
owners when a composition contains ``continuity.json``. The contract describes
editorial beats in ONE persistent HyperFrames composition, not concatenated
clips. Shared temporal QA owns browser readiness, seeking, capture and teardown.
This module owns contract validation, DOM-carrier observations and bounded
source digests; ``recheck_continuity_source`` fences the render owner's receipt
without another capture. Evidence is heuristic, never a certificate of artistic
quality or pixel coverage.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
import stat

CONTINUITY_VERSION = 'motion-continuity-v1'
MAX_BEATS = 24
MAX_PLAN_BYTES = 64 * 1024
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')

__all__ = ['CONTINUITY_VERSION', 'validate_continuity_plan',
           'check_continuity_project', 'recheck_continuity_source']


def _number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_continuity_plan(plan: object) -> list[str]:
    """Reject incomplete plans; absence of boundaries can never score a pass."""
    if not isinstance(plan, dict):
        return ['continuity plan must be an object']
    errors = []
    if plan.get('version') != CONTINUITY_VERSION:
        errors.append(f'version must be {CONTINUITY_VERSION}')
    duration = plan.get('duration_s')
    if not _number(duration) or not 0 < duration <= 180:
        errors.append('duration_s must be finite and within (0, 180]')
    beats = plan.get('beats')
    if not isinstance(beats, list) or not 2 <= len(beats) <= MAX_BEATS:
        return errors + [f'beats must contain 2..{MAX_BEATS} editorial beats']
    cursor = 0.0
    seen = set()
    for index, beat in enumerate(beats):
        label = f'beat {index + 1}'
        if not isinstance(beat, dict):
            errors.append(f'{label}: must be an object')
            continue
        beat_id = beat.get('id')
        if not isinstance(beat_id, str) or not _ID.fullmatch(beat_id):
            errors.append(f'{label}: id must be a short DOM-safe identifier')
        elif beat_id in seen:
            errors.append(f'{label}: duplicate beat id {beat_id}')
        else:
            seen.add(beat_id)
        purpose = beat.get('purpose')
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 400:
            errors.append(f'{label}: purpose must contain 1..400 characters')
        start, end = beat.get('start_s'), beat.get('end_s')
        if not _number(start) or not _number(end) or end - start < 0.25:
            errors.append(f'{label}: finite start/end and at least 0.25s required')
        else:
            if abs(start - cursor) > 0.001:
                errors.append(f'{label}: beats must be contiguous from t=0')
            cursor = end
        handoff = beat.get('handoff')
        if index == 0:
            if handoff is not None:
                errors.append(f'{label}: opening beat cannot have a handoff')
            continue
        if not isinstance(handoff, dict):
            errors.append(f'{label}: explicit incoming handoff required')
            continue
        kind = handoff.get('kind')
        if kind not in ('transform', 'hold', 'cut'):
            errors.append(f'{label}: handoff kind must be transform, hold or cut')
        reason = handoff.get('reason')
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 400:
            errors.append(f'{label}: handoff reason must contain 1..400 characters')
        carrier = handoff.get('carrier_id')
        if kind == 'cut':
            if carrier is not None:
                errors.append(f'{label}: a cut must not claim a carrier')
        elif not isinstance(carrier, str) or not _ID.fullmatch(carrier):
            errors.append(f'{label}: handoff requires a DOM carrier_id (not a selector)')
    if _number(duration) and abs(cursor - duration) > 0.001:
        errors.append('beats must cover the exact declared duration_s')
    return errors


# Identity is held in a Playwright JSHandle, not a page-global author declaration.
# Rectangles are screen-space so camera motion participates in the measurements.
_PROBE_JS = """({id, state}) => {
  const root = document.querySelector('body > [data-composition-id]');
  const nodes = Array.from(document.querySelectorAll('[id]')).filter(n => n.id === id);
  if (nodes.length !== 1) return {error: 'carrier must resolve to exactly one DOM node'};
  const node = nodes[0];
  if (!root || node === root || !root.contains(node))
    return {error: 'carrier must be a descendant of the composition root'};
  if (node.tagName.toLowerCase() === 'canvas')
    return {error: 'canvas internals are unsupported; use a persistent DOM/SVG carrier'};
  if (!state.identities.has(node)) state.identities.set(node, state.next++);
  const rect = node.getBoundingClientRect();
  let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
  let right = Math.min(innerWidth, rect.right), bottom = Math.min(innerHeight, rect.bottom);
  let opacity = 1;
  const style = getComputedStyle(node);
  let visible = style.visibility === 'visible';
  for (let p = node; p; p = p.parentElement) {
    const s = getComputedStyle(p);
    opacity *= Number(s.opacity);
    if (s.display === 'none' || s.contentVisibility === 'hidden') visible = false;
    if (p !== node) {
      const r = p.getBoundingClientRect();
      if (s.overflowX !== 'visible') { left = Math.max(left, r.left); right = Math.min(right, r.right); }
      if (s.overflowY !== 'visible') { top = Math.max(top, r.top); bottom = Math.min(bottom, r.bottom); }
    }
  }
  const area = Math.max(0, right - left) * Math.max(0, bottom - top);
  return {identity: state.identities.get(node), visible, opacity,
          x: rect.x + rect.width / 2, y: rect.y + rect.height / 2,
          width: rect.width, height: rect.height,
          area_ratio: area / (innerWidth * innerHeight),
          frame_width: innerWidth, frame_height: innerHeight};
}"""


def _delta(a: dict, b: dict) -> tuple[float, float]:
    diagonal = math.hypot(a['frame_width'], a['frame_height'])
    distance = math.hypot(a['x'] - b['x'], a['y'] - b['y']) / diagonal
    scale = max(abs(math.log(max(0.001, b[key]) / max(0.001, a[key])))
                for key in ('width', 'height'))
    return distance, scale


def _boundary_errors(samples: list[dict], kind: str) -> list[str]:
    errors = []
    if len(samples) != 7:
        return ['incomplete boundary samples']
    for sample in samples:
        if sample.get('error'):
            errors.append(sample['error'])
            continue
        fields = ('opacity', 'x', 'y', 'width', 'height', 'area_ratio',
                  'frame_width', 'frame_height')
        if not all(_number(sample.get(key)) for key in fields):
            errors.append('carrier measurement is incomplete or non-finite')
        elif (not sample.get('visible') or sample['opacity'] < 0.1
              or sample['area_ratio'] < 0.001 or sample['width'] <= 0
              or sample['height'] <= 0 or sample['frame_width'] <= 0
              or sample['frame_height'] <= 0):
            errors.append('carrier disappears, is clipped/offscreen, or is too small to read')
        elif sample['area_ratio'] > 0.85:
            errors.append('carrier covers almost the whole frame; declare a focal object, not the background')
    if errors:
        return list(dict.fromkeys(errors))
    if len({sample['identity'] for sample in samples}) != 1:
        errors.append('carrier DOM identity was replaced across the handoff or reverse seek')
    # The three close samples bracket the declared boundary, catching abrupt
    # resets without confusing a deliberate fast move over the wider window.
    for a, b in ((samples[1], samples[2]), (samples[2], samples[3])):
        distance, scale = _delta(a, b)
        if distance > 0.12 or scale > 0.35:
            errors.append('carrier jumps at the boundary; preserve its screen-space state')
            break
    if kind == 'transform':
        distance, scale = _delta(samples[0], samples[4])
        if distance < 0.01 and scale < 0.05:
            errors.append('transform handoff has no measurable position/size change; use hold if intentional')
    # Screenshots, not only boxes: forward and backward seeks must reproduce
    # the same pixels. No wall-clock animation gets a free pass from stable DOM.
    for original, replay in ((0, 5), (4, 6)):
        if samples[original]['pixel_digest'] != samples[replay]['pixel_digest']:
            errors.append('reverse seek changed pixels at the same time; make animation time-addressable')
            break
    return errors


def _require_supported_os():
    """Fail closed: there is no safe follow-links or unlocked fallback."""
    import fcntl

    if not (os.O_NONBLOCK and os.O_NOFOLLOW and os.O_DIRECTORY
            and callable(fcntl.flock) and fcntl.LOCK_EX and fcntl.LOCK_NB):
        raise NotImplementedError('continuity requires nonblocking, nofollow and flock primitives')
    # os.replace uses rename's dir_fd capability; Python lists os.rename,
    # not its replace alias, in supports_dir_fd.
    if not {os.open, os.unlink, os.rename}.issubset(os.supports_dir_fd):
        raise NotImplementedError('continuity requires dir_fd support for open, unlink and rename')
    return fcntl


def _remove_pending(fd: int):
    """Reclaim only this recipe's bounded reconstructible scratch names."""
    for index in range(1, MAX_BEATS):
        try:
            os.unlink(f'.boundary-{index:03d}.png.pending', dir_fd=fd)
        except FileNotFoundError:
            pass


@contextmanager
def _evidence_directory(project_dir: str):
    """Pin one private directory; concurrent checks cannot mix preview receipts."""
    fcntl = _require_supported_os()

    path = Path(project_dir) / '.tofu-continuity'
    path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _remove_pending(fd)
        try:
            yield path, fd
        finally:
            _remove_pending(fd)
    finally:
        os.close(fd)


def _publish_preview(directory: Path, fd: int, name: str, raw: bytes) -> str:
    def check_directory():
        actual, pinned = directory.stat(follow_symlinks=False), os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise RuntimeError('continuity evidence directory changed during capture')
    check_directory()
    temporary = '.' + name + '.pending'
    # The directory lock makes this fixed, bounded reconstructible scratch name
    # safe, including recovery after a killed worker. Never follow a leaf link.
    try:
        os.unlink(temporary, dir_fd=fd)
    except FileNotFoundError:
        pass
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                  0o600, dir_fd=fd)
    try:
        with os.fdopen(out, 'wb') as handle:
            handle.write(raw)
        check_directory()
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        check_directory()
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
    return str(directory / name)


def _check_plan(project_dir: str, plan: dict, abort_event, deadline: float) -> dict:
    with _evidence_directory(project_dir) as (directory, fd):
        return _capture_plan(project_dir, plan, abort_event, deadline, directory, fd)


def _capture_plan(project_dir: str, plan: dict, abort_event, deadline: float,
                  evidence_dir: Path, evidence_fd: int) -> dict:
    from lib.design_sys.temporal_qa import screenshot_timeline_contact_sheet

    boundaries = []
    duration = float(plan['duration_s'])
    beats = plan['beats']
    for index, beat in enumerate(beats[1:], 1):
        if abort_event is not None and abort_event.is_set():
            raise InterruptedError('continuity check aborted')

        if time.monotonic() >= deadline:
            raise TimeoutError('continuity check deadline exhausted')
        handoff = beat['handoff']
        kind = handoff['kind']
        boundary = {'beat_id': beat['id'], 'at_s': beat['start_s'],
                    'kind': kind, 'reason': handoff['reason']}
        if kind == 'cut':
            boundaries.append(dict(boundary, status='declared_cut', errors=[]))
            continue
        at = float(beat['start_s'])
        window = min(0.2, (at - beats[index - 1]['start_s']) / 4,
                     (beat['end_s'] - at) / 4)
        near = min(1 / 60, window / 4)
        seconds = [at - window, at - near, at, at + near, at + window,
                   at - window, at + window]
        samples = []
        state = None

        def observe(page, sample_time, raw):
            nonlocal state
            if state is None:
                state = page.evaluate_handle('({identities: new WeakMap(), next: 1})')
            sample = page.evaluate(_PROBE_JS, {'id': handoff['carrier_id'], 'state': state})
            sample['time_s'] = sample_time
            sample['pixel_digest'] = hashlib.sha256(raw).hexdigest()
            samples.append(sample)

        image = screenshot_timeline_contact_sheet(
            project_dir, None, progresses=[t / duration for t in seconds],
            duration_s=duration, settle_ms=0,
            abort_check=abort_event.is_set if abort_event is not None else None,
            sample_observer=observe, deadline=deadline)
        output = _publish_preview(evidence_dir, evidence_fd, f'boundary-{index:03d}.png', image)
        errors = _boundary_errors(samples, kind)
        boundaries.append(dict(boundary, carrier_id=handoff['carrier_id'],
                               status='failed' if errors else 'sampled_pass',
                               errors=errors, contact_sheet=output,
                               contact_sheet_sha256=hashlib.sha256(image).hexdigest(),
                               sample_times_s=seconds))
    checked = [b for b in boundaries if b['kind'] != 'cut']
    errors = [f"{b['beat_id']}: {error}" for b in checked for error in b['errors']]
    if not checked:
        errors.append('no carried boundaries were declared; cuts alone do not demonstrate continuity')
    return {'ok': not errors, 'status': 'failed' if errors else 'sampled_pass',
            'version': CONTINUITY_VERSION, 'boundaries': boundaries,
            'checked_boundaries': len(checked),
            'declared_cuts': len(boundaries) - len(checked), 'errors': errors,
            'limitations': 'Sampled DOM/SVG identity, geometry and seek repeatability only. '
                           'Not an artistic score, occlusion/mask proof, or a check of undeclared cuts. '
                           'Inspect the contact sheets and watch the rendered film.'}


def _unavailable(detail: str, category: str = 'infra') -> dict:
    return {'ok': False, 'status': 'aborted' if category == 'aborted' else 'unavailable',
            'category': category, 'errors': [detail[:500]]}


def _read_regular(path: Path, limit: int) -> bytes:
    # Nonblocking admission prevents a FIFO/device from hanging the parent
    # before it can enter the killable subprocess boundary.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'{path.name} must be a regular file')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError(f'{path.name} exceeds continuity byte limit ({limit} bytes)')
    return raw


def _load_contract(project_dir: str) -> tuple[dict | None, dict | None]:
    path = Path(project_dir) / 'continuity.json'
    if not os.path.lexists(path):
        return None, {'ok': True, 'status': 'not_requested'}
    try:
        _require_supported_os()
        raw = _read_regular(path, MAX_PLAN_BYTES)
        plan = json.loads(raw)
        errors = validate_continuity_plan(plan)
    except (ValueError, UnicodeError, RecursionError) as exc:
        errors = [str(exc)[:500]]
    except (OSError, AttributeError, ImportError, NotImplementedError) as exc:
        return None, _unavailable(f'continuity contract unavailable: {exc}')
    if errors:
        return None, {'ok': False, 'status': 'invalid_contract',
                      'category': 'continuity_failure', 'errors': errors}
    return plan, None


def _source_digest(project_dir: str) -> str:
    digest = hashlib.sha256()
    for name, limit in (('index.html', 1024 * 1024), ('continuity.json', MAX_PLAN_BYTES)):
        raw = _read_regular(Path(project_dir) / name, limit)
        digest.update(name.encode() + b'\x00' + raw)
    return digest.hexdigest()


def recheck_continuity_source(project_dir: str, receipt: dict) -> dict:
    """Revalidate a sampled receipt without another worker or browser capture."""
    expected = receipt.get('source_sha256')
    if not isinstance(expected, str) or not expected:
        return _unavailable('continuity receipt is missing the checked source digest')
    try:
        if _source_digest(project_dir) != expected:
            return _unavailable('composition changed since continuity capture; check the current source again')
    except (OSError, ValueError, AttributeError, NotImplementedError) as exc:
        return _unavailable(f'continuity source unavailable: {exc}')
    return receipt


_WORKER_RECEIPT = 'TOFU_CONTINUITY_RESULT='


def _worker_check(project_dir: str, timeout: float, expected_digest: str) -> dict:
    """Called only in the render owner's killable process group, never a pool."""
    plan, early = _load_contract(project_dir)
    if early is not None:
        return early if not early['ok'] else _unavailable('continuity contract disappeared')
    try:
        if _source_digest(project_dir) != expected_digest:
            return _unavailable('composition changed before continuity capture')
        result = _check_plan(project_dir, plan, None, time.monotonic() + timeout)
        if _source_digest(project_dir) != expected_digest:
            return _unavailable('composition changed during continuity capture')
        result['source_sha256'] = expected_digest
        if not result['ok']:
            result['category'] = 'continuity_failure'
        return result
    except TimeoutError:
        return _unavailable('continuity check deadline exhausted', 'timeout')
    except Exception as exc:
        # Sampler capacity limits, runtime errors and unavailable dependencies
        # are missing evidence, NOT malformed author contracts.
        return _unavailable(f'continuity evidence unavailable: {exc}')


def check_continuity_project(project_dir: str, *, abort_event=None,
                             timeout: float = 300) -> dict:
    """Check an opted-in film; ordinary scene projects remain unchanged.

    Shared render subprocess ownership supplies hard timeout/abort + process-
    group teardown even when an author's synchronous JS seek never returns.
    Shared temporal QA still owns capture; no new renderer, pool or job exists.
    Previews use at most MAX_BEATS-1 fixed paths and atomic replacement.
    """
    start = time.monotonic()
    project_dir = os.path.abspath(project_dir)
    plan, early = _load_contract(project_dir)
    if early is not None:
        return early
    if abort_event is not None and abort_event.is_set():
        return _unavailable('continuity check aborted', 'aborted')
    if not _number(timeout) or timeout <= 0:
        return _unavailable('continuity check deadline exhausted', 'timeout')
    if (Path(project_dir) / '.tofu-continuity').is_symlink():
        return _unavailable('continuity evidence directory must not be a symlink')
    # A plan containing only cuts requires no browser and cannot claim success.
    if all(beat['handoff']['kind'] == 'cut' for beat in plan['beats'][1:]):
        return {'ok': False, 'status': 'failed', 'category': 'continuity_failure',
                'checked_boundaries': 0, 'declared_cuts': len(plan['beats']) - 1,
                'boundaries': [dict(beat_id=b['id'], at_s=b['start_s'], kind='cut',
                                    reason=b['handoff']['reason'], status='declared_cut', errors=[])
                               for b in plan['beats'][1:]],
                'errors': ['no carried boundaries were declared; cuts alone do not demonstrate continuity']}
    try:
        digest = _source_digest(project_dir)
    except (OSError, ValueError, AttributeError, NotImplementedError) as exc:
        return _unavailable(f'continuity evidence unavailable: {exc}')
    remaining = timeout - (time.monotonic() - start)
    if remaining <= 0:
        return _unavailable('continuity check deadline exhausted', 'timeout')
    from lib.motion_video._render import _run_cli
    run = _run_cli([sys.executable, '-m', 'lib.motion_video._continuity', project_dir,
                    str(remaining), digest], cwd=str(Path(__file__).resolve().parents[2]),
                   timeout=remaining, abort_event=abort_event)
    # A hard kill cannot execute the child's finally block. Reclaim its fixed
    # scratch names under the same nonblocking directory lock; an already-
    # running successor owns cleanup instead. Never touch completed previews.
    cleanup_unavailable = ''
    try:
        with _evidence_directory(project_dir):
            pass
    except (AttributeError, ImportError, NotImplementedError) as exc:
        cleanup_unavailable = f'continuity evidence primitives unavailable: {exc}'
    except OSError:
        # No-follow admission also applies during cleanup. A renamed/locked
        # directory is not authority to chase links or delete another run's data.
        pass
    if run['category']:
        return _unavailable(f"continuity worker {run['category']}", run['category'])
    if cleanup_unavailable:
        return _unavailable(cleanup_unavailable)
    if run['rc'] != 0 or len(run['out']) > 256 * 1024:
        return _unavailable('continuity worker failed without bounded evidence')
    try:
        line = next(line for line in reversed(run['out'].splitlines())
                    if line.startswith(_WORKER_RECEIPT))
        result = json.loads(line[len(_WORKER_RECEIPT):])
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise ValueError('invalid worker receipt')
        if result['ok'] and result.get('source_sha256') != digest:
            raise ValueError('worker receipt does not match the checked source')
        return result
    except (StopIteration, ValueError) as exc:
        return _unavailable(f'continuity worker receipt unavailable: {exc}')


if __name__ == '__main__':
    # Private subprocess protocol: callers use check_continuity_project().
    print(_WORKER_RECEIPT + json.dumps(_worker_check(sys.argv[1], float(sys.argv[2]), sys.argv[3])))
