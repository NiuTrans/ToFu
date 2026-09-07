"""lib/motion_video/_render.py — HyperFrames CLI subprocess wrapper.

Runs the ``hyperframes`` CLI (lint / validate / inspect / render) as a
subprocess with the managed environment from :mod:`lib.motion_video._env`
(ffmpeg on PATH, ``HYPERFRAMES_BROWSER_PATH``, conda GUI libs on
``LD_LIBRARY_PATH``), a wall-clock timeout, and cooperative abort via a
``threading.Event`` (the task runtime's ``abort_event``).

Every public function returns a plain dict result (never raises) so the
agent's self-repair loop gets structured feedback:

    {'ok': bool, 'category': str, 'detail': str, ...}

``category`` is one of :data:`ALL_CATEGORIES` — the structured failure
diagnosis the design doc calls for (auto-motion leaves you reading raw
logs; we classify).

**The category taxonomy is a PARTITION with a single owner (this module).**
Every category is either an INFRASTRUCTURE outcome (the toolchain, the host or
the user stopped us — says nothing about the composition) or a COMPOSITION
verdict (the frame is genuinely wrong). Consumers MUST ask
:func:`is_infra_category` instead of hand-writing a tuple of strings.

That rule is written in blood, twice. ``'infra'`` was added on 2026-07-28 to
stop cgroup-memory noise being charged to the author — but it was added to the
CLASSIFIER only, while both consumers
(``engine._scene_gate_findings``, ``_scene_author._full_gate``) kept their own
hand-copied ``('env_missing', 'aborted', 'timeout', 'chrome')``. So the new
category existed and was exempted NOWHERE: measured 2026-07-29, 14 of 14 scene
dirs on this host classified ``'infra'``, and two scenes of a 6-scene film had
7.5 KB / 10.5 KB of finished, gate-clean authored work thrown away and replaced
with a 2-node gradient card. A taxonomy split across three files always drifts;
this module now owns it and :mod:`tests.test_motion_video_gate_categories`
fails if a category is left unclassified.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['lint_project', 'validate_project', 'inspect_project',
           'check_project', 'render_project', 'is_infra_category',
           'INFRA_CATEGORIES', 'COMPOSITION_CATEGORIES', 'ALL_CATEGORIES']

#: Failure categories that describe the ENVIRONMENT, not the composition.
#:
#: A consumer that judges composition quality must treat these as "no verdict
#: available" and fall back to whatever it can compute itself — never as a
#: reason to discard the author's work.
#:
#: ``'io'`` belongs here because the only way a gate call yields it is a failed
#: ``Popen`` (the CLI could not be spawned at all).
INFRA_CATEGORIES = frozenset({
    'env_missing',   # hyperframes CLI / ffmpeg absent
    'aborted',       # cooperative user stop
    'timeout',       # wall-clock budget exceeded
    'chrome',        # headless browser could not boot
    'infra',         # cgroup memory pressure / OOM — host, not composition
    'io',            # the CLI could not be spawned
})

#: Failure categories that DO name a composition defect.
#:
#: ``'unknown'`` is deliberately here rather than in the infra set: it means
#: the CLI failed, named no finding, and matched no stderr heuristic. Treating
#: an unexplained failure as "safe to ignore" would silently forgive real
#: defects, so it stays a composition verdict until something identifies it.
#: If it ever becomes common, classify it — do not move it wholesale.
COMPOSITION_CATEGORIES = frozenset({'lint', 'unknown'})

#: Every category this module can emit. The two sets above MUST partition it —
#: a category in neither is a category some consumer will silently mishandle.
ALL_CATEGORIES = INFRA_CATEGORIES | COMPOSITION_CATEGORIES


def is_infra_category(category: str | None) -> bool:
    """True when ``category`` is an infrastructure outcome, not a verdict.

    The single predicate both gate consumers use. Passing an empty category
    (a clean run) returns False — there is nothing to exempt.
    """
    return (category or '') in INFRA_CATEGORIES


_TAIL = 1500


def _run_cli(args: list[str], *, cwd: str, timeout: int,
             abort_event=None, env: dict | None = None) -> dict:
    """Run ``hyperframes <args>`` with timeout + abort polling.

    Returns ``{'rc': int|None, 'out': str, 'err': str, 'elapsed': float,
    'category': str}`` — category is '' on a clean exit classification.
    """
    from lib.motion_video._env import build_render_env
    env = env or build_render_env()
    start = time.time()
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
    except FileNotFoundError as _e:
        logger.debug('run cli: missing (%s)', _e)
        return {'rc': None, 'out': '', 'err': f'executable not found: {args[0]}',
                'elapsed': 0.0, 'category': 'env_missing'}
    except Exception as e:
        logger.error('[MotionVideo] spawn failed for %s: %s', args[0], e, exc_info=True)
        return {'rc': None, 'out': '', 'err': str(e), 'elapsed': 0.0, 'category': 'io'}

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    category = ''
    deadline = start + timeout if timeout and timeout > 0 else None
    while True:
        try:
            out, err = proc.communicate(timeout=1.0)
            out_chunks.append(out or '')
            err_chunks.append(err or '')
            break
        except subprocess.TimeoutExpired:
            if abort_event is not None and abort_event.is_set():
                category = 'aborted'
                break
            if deadline is not None and time.time() > deadline:
                category = 'timeout'
                break
    if category:
        logger.warning('[MotionVideo] %s %s — killing process group %s',
                       args[0], category, proc.pid)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception as e:
            logger.debug('[MotionVideo] killpg failed: %s', e)
        try:
            out, err = proc.communicate(timeout=10)
            out_chunks.append(out or '')
            err_chunks.append(err or '')
        except Exception as e:
            logger.debug('[MotionVideo] communicate-after-kill failed: %s', e)
    return {'rc': proc.poll(), 'out': ''.join(out_chunks),
            'err': ''.join(err_chunks), 'elapsed': time.time() - start,
            'category': category}


def _classify_failure(res: dict) -> str:
    """Map a non-zero CLI result onto a failure category."""
    if res['category']:
        return res['category']
    blob = (res['err'] + '\n' + res['out']).lower()
    if 'ffmpeg not found' in blob or 'ffprobe not found' in blob:
        return 'env_missing'
    if 'chrome' in blob or 'browser' in blob or 'puppeteer' in blob:
        return 'chrome'
    # Memory pressure is the other way a headless-Chrome boot dies without
    # ever naming itself: under cgroup pressure the CLI's own memory notice is
    # all that reaches stderr (measured 2026-07-28 — the shipped 0-of-8 job).
    # Left as 'unknown' it was charged to the composition author.
    if 'cgroup memory' in blob or 'out of memory' in blob \
            or 'oom' in blob.split() or 'enomem' in blob:
        return 'infra'
    return 'unknown'


def _cli_or_env_error() -> str:
    """Resolve the hyperframes binary or return '' (caller builds env_missing)."""
    from lib.motion_video._env import hyperframes_bin
    return hyperframes_bin()


def _env_missing_result(tool: str) -> dict:
    return {'ok': False, 'category': 'env_missing',
            'detail': 'hyperframes CLI not found — run motion_video_env_check '
                      f'with install=true first (before {tool})'}


# ── Quality gates (lint / validate / inspect) ─────────────

#: Sub-report keys the CLI nests its findings under (``check`` merges three
#: gates into one payload; the individual subcommands use their own key).
_REPORT_SECTIONS = ('lint', 'runtime', 'layout')


def _collect_findings(data) -> tuple[list, bool]:
    """Pull findings out of a CLI JSON report, flat or nested.

    ``hyperframes check`` reports ``{ok, lint:{findings…}, runtime:{findings…},
    layout:{findings…}}`` while the older single-purpose subcommands report a
    top-level ``findings`` list. Reading only the flat key silently discarded
    EVERY finding of the merged report — a composition with a fatal runtime
    error came back ``ok=False`` with an empty error list, which upstream then
    rendered as "no problems found". Returns ``(findings, saw_report)``;
    ``saw_report`` is False when the payload carried no recognisable report, so
    the caller can fall back to the exit code instead of trusting an empty list.
    """
    if not isinstance(data, dict):
        return [], False
    findings: list = []
    saw = False
    if isinstance(data.get('findings'), list):
        findings.extend(data['findings'])
        saw = True
    for key in _REPORT_SECTIONS:
        section = data.get(key)
        if isinstance(section, dict) and isinstance(section.get('findings'), list):
            for f in section['findings']:
                if isinstance(f, dict):
                    f = dict(f)
                    f.setdefault('_section', key)
                findings.append(f)
            saw = True
    return findings, saw


def _gate(subcmd: str, project_dir: str, *, timeout: int,
          abort_event=None) -> dict:
    cli = _cli_or_env_error()
    if not cli:
        return _env_missing_result(subcmd)
    res = _run_cli([cli, subcmd, project_dir, '--json'], cwd=project_dir,
                   timeout=timeout, abort_event=abort_event)
    if res['category']:
        return {'ok': False, 'category': res['category'],
                'detail': (res['err'] or res['out'])[-_TAIL:]}
    findings: list[dict] = []
    parse_note = ''
    saw_report = False
    report_ok = None
    try:
        data = json.loads(res['out'])
        findings, saw_report = _collect_findings(data)
        if saw_report and isinstance(data.get('ok'), bool):
            report_ok = data['ok']
    except Exception as _e:
        # Non-JSON output (older CLI / human format): fall back to exit code.
        logger.debug('gate: failed (%s)', _e)
        parse_note = 'non-json output; gated on exit code'
    errors = [f for f in findings if f.get('severity') == 'error']

    # ── Two INDEPENDENT verdict sources: the report and the exit code ──
    # The CLI's machine-readable report is the authority on COMPOSITION
    # quality; the exit code also fires for things that have nothing to do
    # with the composition. Measured 2026-07-28 (the 0-of-8 job): under cgroup
    # memory pressure the CLI emitted a complete report saying ok=true with
    # zero findings and STILL exited 1, and because its stderr named only its
    # own memory notice the failure classified as 'unknown' — which the
    # engine's infrastructure exemption does not cover. Six good authored
    # compositions were thrown away and the film shipped as eight gradient
    # cards.
    #
    # So: when the CLI told us in its own report that the composition passed,
    # believe the report. A non-zero exit alongside it is an INFRASTRUCTURE
    # event, logged and classified as such — never charged to the author.
    if report_ok is True and not errors:
        if res['rc'] != 0:
            logger.warning(
                '[MotionVideo] %s exited %s but its JSON report says ok with '
                'zero findings — trusting the report and classifying the '
                'non-zero exit as infrastructure (stderr: %.200s)',
                subcmd, res['rc'], (res['err'] or '').strip())
        ok = True
    else:
        ok = res['rc'] == 0 and not errors
    if not ok and not errors:
        # The CLI failed but named no error-severity finding (or its report
        # was unparseable). Surface the exit code + stderr as ONE synthetic
        # finding: a failure carrying an empty reason list reads as SUCCESS to
        # every consumer that inspects only the errors.
        detail = (res['err'] or res['out'] or '').strip()[-_TAIL:]
        errors = [{'message': (
            f'{subcmd} failed (exit {res["rc"]}) without a machine-readable '
            f'finding' + (f': {detail}' if detail else '')),
            'severity': 'error'}]
        findings = list(findings) + errors
        if not saw_report and not parse_note:
            parse_note = 'report carried no recognisable findings section'
        # No named finding => the failure is UNEXPLAINED, so the stderr
        # heuristics are the only evidence of what went wrong.
        category = 'lint' if subcmd == 'lint' else _classify_failure(res)
    elif not ok:
        # The report NAMED a composition defect. That verdict owns the
        # category: running the stderr heuristics here would relabel a real
        # font/contrast/overflow error as 'infra' or 'chrome' merely because
        # the CLI also printed a memory notice — and both consumers
        # (engine._scene_gate_findings, _scene_author._full_gate) EXEMPT those
        # categories, which would silently forgive the very defects this gate
        # exists to catch.
        category = 'lint'
    else:
        category = ''
    return {
        'ok': ok,
        'category': category,
        'errors': [f.get('message', '') for f in errors],
        'warnings': [f.get('message', '') for f in findings
                     if f.get('severity') == 'warning'],
        'fix_hints': [f.get('fixHint', '') for f in errors if f.get('fixHint')],
        'detail': parse_note or (res['err'][-_TAIL:] if not ok else ''),
        'elapsed': round(res['elapsed'], 2),
    }


def lint_project(project_dir: str, *, timeout: int = 180, abort_event=None) -> dict:
    """``hyperframes lint`` — static composition checks."""
    return _gate('lint', project_dir, timeout=timeout, abort_event=abort_event)


def validate_project(project_dir: str, *, timeout: int = 300, abort_event=None) -> dict:
    """``hyperframes validate`` — headless-Chrome runtime errors + contrast."""
    return _gate('validate', project_dir, timeout=timeout, abort_event=abort_event)


def inspect_project(project_dir: str, *, timeout: int = 300, abort_event=None) -> dict:
    """``hyperframes inspect`` — layout overflow across timeline samples."""
    return _gate('inspect', project_dir, timeout=timeout, abort_event=abort_event)


def check_project(project_dir: str, *, timeout: int = 300, abort_event=None) -> dict:
    """Run all three static gates and merge the results.

    Prefers the CLI's own ``check`` subcommand, which runs lint + runtime
    validation + layout inspection in ONE headless-Chrome boot (``validate``
    and ``inspect`` are each separately deprecated, and three boots cost ~3×
    the wall time). Falls back to the legacy per-gate calls when ``check`` is
    unavailable, so an older pinned CLI keeps working.
    """
    merged = {'ok': True, 'category': '', 'errors': [], 'warnings': [],
              'fix_hints': [], 'gates': {}, 'elapsed': 0.0}

    combined = _gate('check', project_dir, timeout=timeout,
                     abort_event=abort_event)
    if combined.get('category') != 'env_missing' \
            and 'unknown command' not in (combined.get('detail') or '').lower():
        combined['ok'] = bool(combined.get('ok'))
        merged.update({
            'ok': combined['ok'],
            'category': '' if combined['ok'] else (combined.get('category') or 'unknown'),
            'errors': list(combined.get('errors', [])),
            'warnings': list(combined.get('warnings', [])),
            'fix_hints': [h for h in combined.get('fix_hints', []) if h],
            'gates': {'check': combined},
            'elapsed': round(combined.get('elapsed', 0.0), 2),
        })
        return merged

    for name, fn in (('lint', lint_project),
                     ('validate', validate_project),
                     ('inspect', inspect_project)):
        r = fn(project_dir, timeout=timeout, abort_event=abort_event)
        merged['gates'][name] = r
        merged['elapsed'] += r.get('elapsed', 0.0)
        merged['errors'].extend(f'[{name}] {e}' for e in r.get('errors', []))
        merged['warnings'].extend(f'[{name}] {w}' for w in r.get('warnings', []))
        merged['fix_hints'].extend(h for h in r.get('fix_hints', []) if h)
        if not r.get('ok'):
            merged['ok'] = False
            if not merged['category']:
                merged['category'] = r.get('category', 'unknown')
            # env_missing / aborted / timeout are fatal — stop early.
            if r.get('category') in ('env_missing', 'aborted', 'timeout'):
                break
    merged['elapsed'] = round(merged['elapsed'], 2)
    return merged


# ── Render ────────────────────────────────────────────────

_RENDERED_RE = re.compile(r'rendered in ([0-9.]+)s')


def render_project(project_dir: str, output: str, *, quality: str = 'standard',
                   fps: int | None = None, timeout: int = 1800,
                   abort_event=None) -> dict:
    """``hyperframes render`` one composition project → MP4.

    Args:
        project_dir: hyperframes project dir. Holding ``index.html`` is
            sufficient — the CLI needs no project manifest and the engine
            writes only that file per scene.
        output: MP4 output path (absolute, or relative to project_dir).
        quality: draft | standard | high.
        fps: optional 24/30/60 override.
        timeout: wall-clock seconds (0 = no limit).
        abort_event: optional threading.Event for cooperative cancel.

    Post-checks: the output file exists and is non-empty (the CLI's own
    contract — see the HyperFrames CLI skill's post-render verification).
    """
    cli = _cli_or_env_error()
    if not cli:
        return _env_missing_result('render')
    if quality not in ('draft', 'standard', 'high'):
        return {'ok': False, 'category': 'io',
                'detail': f'invalid quality {quality!r} (draft|standard|high)'}
    args = [cli, 'render', project_dir, '--quality', quality, '-o', output]
    if fps:
        args += ['--fps', str(fps)]
    logger.info('[MotionVideo] render start: %s (quality=%s fps=%s)',
                project_dir, quality, fps or 'default')
    res = _run_cli(args, cwd=project_dir, timeout=timeout, abort_event=abort_event)
    ok = res['rc'] == 0 and not res['category'] \
        and os.path.isfile(output) and os.path.getsize(output) > 0
    rendered_s = None
    m = _RENDERED_RE.search(res['out'])
    if m:
        rendered_s = float(m.group(1))
    result = {
        'ok': ok,
        'category': '' if ok else _classify_failure(res),
        'output': output if ok else '',
        'elapsed': round(res['elapsed'], 2),
        'render_time_s': rendered_s,
        'detail': '' if ok else (res['err'] or res['out'])[-_TAIL:],
    }
    if ok:
        logger.info('[MotionVideo] render done: %s (%.1fs wall)', output, res['elapsed'])
    else:
        logger.warning('[MotionVideo] render failed (%s): %.500s',
                       result['category'], result['detail'])
    return result
