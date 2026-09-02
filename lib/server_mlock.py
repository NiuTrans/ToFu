"""mlockall gate helpers (extracted from server.py).

Co-location requirement: ``tests/test_mlock_headroom_gate.py`` imports this
module and monkeypatches ``_tofu_path_is_fuse`` / ``_tofu_cgroup_mem_limit_bytes``
/ ``_tofu_cgroup_mem_usage_bytes``, then calls ``_tofu_should_mlock()``. All four
must therefore live in this one module object.
"""

import os
import sys


def _tofu_path_is_fuse(_path):
    """Best-effort: True if *_path* sits on a FUSE filesystem (stdlib-only)."""
    try:
        _path = os.path.abspath(_path)
        _best_mp, _best_fstype = '', ''
        with open('/proc/self/mountinfo', 'r') as _f:
            for _line in _f:
                # mountinfo: "... <mount point> ... - <fstype> <source> ..."
                _halves = _line.split(' - ')
                if len(_halves) != 2:
                    continue
                _left = _halves[0].split()
                _right = _halves[1].split()
                if len(_left) < 5 or not _right:
                    continue
                _mp, _fstype = _left[4], _right[0]
                if (_path == _mp or _path.startswith(_mp.rstrip('/') + '/')) \
                        and len(_mp) >= len(_best_mp):
                    _best_mp, _best_fstype = _mp, _fstype
        return _best_fstype.startswith('fuse')
    except OSError:
        return False


def _tofu_cgroup_mem_limit_bytes():
    """cgroup memory limit in bytes, or None if unlimited/unknown (stdlib-only)."""
    for _p in ('/sys/fs/cgroup/memory.max',                    # cgroup v2
               '/sys/fs/cgroup/memory/memory.limit_in_bytes'):  # cgroup v1
        try:
            with open(_p, 'r') as _f:
                _raw = _f.read().strip()
        except OSError:
            continue
        if _raw == 'max':
            return None
        try:
            _val = int(_raw)
        except ValueError:
            continue
        # cgroup v1 reports a huge sentinel (~PAGE_COUNTER_MAX) for "unlimited"
        if _val <= 0 or _val >= (1 << 62):
            return None
        return _val
    return None


def _tofu_cgroup_mem_usage_bytes():
    """Current cgroup memory usage in bytes, or None if unknown (stdlib-only).

    Includes reclaimable page cache on purpose: a shared cgroup running at the
    cache edge is exactly the contended, spike-prone state where adding
    unreclaimable pinned pages is net-harmful (see _tofu_should_mlock).
    """
    for _p in ('/sys/fs/cgroup/memory.current',                    # cgroup v2
               '/sys/fs/cgroup/memory/memory.usage_in_bytes'):      # cgroup v1
        try:
            with open(_p, 'r') as _f:
                _raw = _f.read().strip()
        except OSError:
            continue
        try:
            _val = int(_raw)
        except ValueError:
            continue
        if _val < 0:
            return None
        return _val
    return None


def _tofu_should_mlock():
    """Decide whether mlockall is worth it. Returns (do_it, reason)."""
    _mode = os.environ.get('TOFU_MLOCK', 'off').strip().lower()
    if _mode in ('0', 'off', 'false', 'no'):
        return False, 'disabled via TOFU_MLOCK=%s' % _mode
    if _mode in ('1', 'on', 'true', 'yes', 'force'):
        return True, 'forced via TOFU_MLOCK=%s' % _mode
    # auto: pin only where the SIGBUS risk is real (project dir OR the conda
    # env holding the .so files is on FUSE) AND there is enough memory
    # headroom that pinning won't trip the OOM killer.
    # This module lives in lib/, so the project root is one level up.
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _on_fuse = (_tofu_path_is_fuse(_project_root)
                or _tofu_path_is_fuse(sys.prefix))
    if not _on_fuse:
        return False, 'not on FUSE (no SIGBUS risk to mitigate)'
    _limit = _tofu_cgroup_mem_limit_bytes()
    if _limit is None:
        return True, 'on FUSE, cgroup memory unlimited'
    try:
        _min_gb = float(os.environ.get('TOFU_MLOCK_MIN_LIMIT_GB', '8'))
    except ValueError:
        _min_gb = 8.0
    _gib = float(1 << 30)
    if _limit < _min_gb * _gib:
        return False, ('on FUSE but cgroup limit %.1fGiB < %.1fGiB — skipping to avoid '
                       'OOM (set TOFU_MLOCK=1 to force)' % (_limit / _gib, _min_gb))
    # The cgroup limit is generous, but on a SHARED cgroup that ceiling can be
    # the whole machine and already ~full of siblings + FUSE page/slab cache.
    # Pinning here adds unreclaimable pages AND inflates our own oom_score, so
    # the OOM killer picks us first (highest-RSS process in the group). Gate on
    # LIVE headroom: skip if usage already sits above TOFU_MLOCK_MAX_USAGE_PCT
    # (default 85%) of the limit. Unknown usage → proceed (matches prior behaviour).
    _usage = _tofu_cgroup_mem_usage_bytes()
    if _usage is not None and _usage > 0:
        try:
            _max_pct = float(os.environ.get('TOFU_MLOCK_MAX_USAGE_PCT', '85'))
        except ValueError:
            _max_pct = 85.0
        _used_pct = 100.0 * _usage / float(_limit)
        if _used_pct >= _max_pct:
            return False, ('on FUSE but cgroup %.1f%% full (%.1f/%.1fGiB) >= %.0f%% — '
                           'skipping to avoid OOM on a contended shared cgroup '
                           '(set TOFU_MLOCK=1 to force)'
                           % (_used_pct, _usage / _gib, _limit / _gib, _max_pct))
        return True, ('on FUSE, cgroup limit %.1fGiB >= %.1fGiB and %.1f%% used < %.0f%%'
                      % (_limit / _gib, _min_gb, _used_pct, _max_pct))
    return True, 'on FUSE, cgroup limit %.1fGiB >= %.1fGiB (usage unknown)' % (_limit / _gib, _min_gb)
