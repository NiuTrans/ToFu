"""lib/self_update/_requirements.py — dependency (requirements.txt) handling.

``_requirements_changed`` (SHA-range diff) and ``_install_requirements``
(pip install against the running interpreter).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Optional

from lib.log import log_context
from lib.self_update._config import _PIP_TIMEOUT, _REQUIREMENTS, _ROOT
from lib.self_update._git import _run_git

from lib.log import get_logger

logger = get_logger(__name__)

# The pip-failure log is surfaced verbatim in the update dialog (with a copy
# button) so the operator can paste the WHOLE error, not a mangled tail. Keep
# a generous ceiling that still protects the push channel / DB from a runaway
# multi-megabyte install log. The one-line server-log message stays short.
_DEPS_LOG_MAX = 20000

def _requirements_changed(before_sha: Optional[str],
                          after_sha: Optional[str]) -> bool:
    """True if ``requirements.txt`` differs between two commits.

    Compares by SHA range rather than scraping ``git pull`` output, which
    is locale-dependent and unreliable. If either SHA is missing or the
    diff can't be computed, returns True (install defensively rather than
    silently skip a needed dependency).
    """
    if before_sha and after_sha and before_sha == after_sha:
        return False  # nothing was pulled
    if not before_sha or not after_sha:
        logger.warning('[Update] missing commit SHA — will install '
                       'dependencies defensively')
        return True
    try:
        cp = _run_git(['diff', '--name-only', before_sha, after_sha])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] requirements diff failed (%s) — will '
                       'install defensively', e)
        return True
    if cp.returncode != 0:
        logger.warning('[Update] requirements diff returned %d — will '
                       'install defensively', cp.returncode)
        return True
    changed = [p.strip() for p in cp.stdout.splitlines() if p.strip()]
    return _REQUIREMENTS in changed


def _install_requirements(on_line=None) -> dict:
    """Run ``pip install -r requirements.txt`` with the current interpreter.

    Returns ``{'ok': bool, 'detail': str}``. Uses ``sys.executable -m pip``
    so the install targets the SAME interpreter the server runs under
    (critical inside a conda env). Never raises — failures are captured.

    Args:
        on_line: Optional ``fn(line: str)`` invoked for each pip output line
            (e.g. ``Collecting httpx`` / ``Installing collected packages``)
            so a UI can show live activity instead of a spinner that looks
            frozen for minutes. ``--progress-bar off`` keeps pip's own
            carriage-return bars out of the stream (they're line-based here).
    """
    req_path = os.path.join(_ROOT, _REQUIREMENTS)
    if not os.path.isfile(req_path):
        logger.info('[Update] No %s — skipping dependency install',
                    _REQUIREMENTS)
        return {'ok': True, 'detail': 'no requirements.txt'}

    cmd = [sys.executable, '-m', 'pip', 'install', '--progress-bar', 'off',
           '-r', req_path]
    logger.info('[Update] Installing dependencies: %s', ' '.join(cmd))
    with log_context('self_update.pip_install', logger=logger):
        if on_line is None:
            # Simple path: capture whole output, no live streaming.
            try:
                cp = subprocess.run(cmd, cwd=_ROOT, capture_output=True,
                                    text=True, timeout=_PIP_TIMEOUT)
            except (FileNotFoundError, subprocess.TimeoutExpired,
                    subprocess.SubprocessError) as e:
                logger.error('[Update] pip install errored: %s', e, exc_info=True)
                return {'ok': False, 'detail': str(e)[:500]}
            out, rc = (cp.stdout or ''), cp.returncode
            err = cp.stderr or ''
        else:
            # Streaming path: read pip's merged output line-by-line, forward
            # each meaningful line to ``on_line`` for live UI feedback.
            out_lines: list[str] = []
            try:
                proc = subprocess.Popen(
                    cmd, cwd=_ROOT, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            except (FileNotFoundError, OSError) as e:
                logger.error('[Update] pip install errored: %s', e, exc_info=True)
                return {'ok': False, 'detail': str(e)[:500]}

            def _pump():
                stream = proc.stdout
                if stream is None:
                    return
                for line in stream:
                    line = line.rstrip('\n')
                    if not line.strip():
                        continue
                    out_lines.append(line)
                    try:
                        on_line(line)
                    except Exception as e:
                        logger.debug('[Update] pip line cb failed: %s', e)

            pump = threading.Thread(target=_pump, daemon=True)
            pump.start()
            deadline = time.monotonic() + _PIP_TIMEOUT
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    logger.error('[Update] pip install timed out after %ds',
                                 _PIP_TIMEOUT)
                    return {'ok': False,
                            'detail': f'pip install timed out after {_PIP_TIMEOUT}s'}
                time.sleep(0.2)
            pump.join(timeout=3)
            out, rc, err = '\n'.join(out_lines), proc.returncode, ''

    if rc != 0:
        full = (err or out or '')
        # Full log to the UI (bounded); a short tail to the server log line.
        logger.error('[Update] pip install failed (exit %d): %s', rc, full[-500:])
        return {'ok': False, 'detail': full[-_DEPS_LOG_MAX:]}

    logger.info('[Update] Dependencies installed successfully')
    return {'ok': True, 'detail': (out or '')[-300:]}

