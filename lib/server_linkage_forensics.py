"""Boot-time libstdc++ soname binding forensics (extracted from server.py).

The capture body records which ``libstdc++.so.6`` copy owns the soname (or
which one the loader WOULD resolve) plus the two injection variables that can
mis-bind it. ``server.py`` keeps the source-pinned ``libstdc++ soname ->``
diagnostic line + exception guard around this function's result.
"""

import os

from lib.log import get_logger


logger = get_logger(__name__)


def capture_linkage_forensics():
    """Return the ``libstdc++ soname -> … | LD_PRELOAD=… | LD_LIBRARY_PATH=…``
    diagnostic string, or ``libstdc++ soname -> unavailable`` on any failure.

    Diagnostic only: it must never raise, and it changes no behaviour.
    """
    try:
        _stdcxx_paths = []
        with open('/proc/self/maps', 'r') as _mf:
            for _line in _mf:
                if 'libstdc++' in _line:
                    _p = _line.rsplit(' ', 1)[-1].strip()
                    if _p and _p not in _stdcxx_paths:
                        _stdcxx_paths.append(_p)
        if _stdcxx_paths:
            _stdcxx_state = 'mapped=' + ','.join(_stdcxx_paths)
        else:
            # Not yet bound — ask the loader which copy it WOULD pick.
            try:
                import ctypes as _fx_ctypes
                _fx_ctypes.CDLL('libstdc++.so.6')
                _probe = [l.rsplit(' ', 1)[-1].strip()
                          for l in open('/proc/self/maps') if 'libstdc++' in l]
                _seen = []
                for _p in _probe:
                    if _p and _p not in _seen:
                        _seen.append(_p)
                _stdcxx_state = ('would-resolve=' + ','.join(_seen)) if _seen \
                    else 'unresolvable'
            except Exception as _fx_e:
                logger.debug(
                    'libstdc++ resolution probe failed: %s', _fx_e)
                _stdcxx_state = 'probe-failed:%s' % (str(_fx_e)[:80],)
        return ('libstdc++ soname -> %s | LD_PRELOAD=%s | LD_LIBRARY_PATH=%s' % (
            _stdcxx_state,
            (os.environ.get('LD_PRELOAD') or '<unset>'),
            (os.environ.get('LD_LIBRARY_PATH') or '<unset>')))
    except Exception as exc:
        # Forensics must never be able to break a boot it only observes.
        logger.debug('libstdc++ linkage forensics unavailable: %s', exc)
        return 'libstdc++ soname -> unavailable'
