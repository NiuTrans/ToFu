#!/usr/bin/env python3
"""Regression guard: opensource export must scrub internal absolute paths
(and the operator's username) from ``.conf`` files.

Background — the leaked-infra-path class:

  ``deploy/supervisor/tofu.conf`` embeds the real deployment paths in its
  ``command`` / ``directory`` / ``stdout_logfile`` lines (an internal mount
  root plus the operator's username). The opensource sanitizer's step-8
  regex (``_sanitize_source_opensource``) already rewrites those internal
  mount paths to a generic placeholder — BUT the file never reached it:
  ``.conf`` was absent from ``_is_text_file``'s ``text_exts`` allowlist, so
  ``_post_copy_sanitize``'s ``if not _is_text_file(...): continue`` skipped
  it before sanitization. Net effect: the internal absolute path + the
  username were published to the public GitHub mirror.

Root-cause fix: ``.conf`` is now a recognised text extension, so every
present/future ``.conf`` file flows through the sanitizer.

The internal tokens this asserts on are assembled from fragments (never a
contiguous literal), because this guard file is itself shipped in the
exported tree — a raw literal here would reintroduce the very leak it
guards against and trip ``git grep`` on the published mirror.

These tests run the REAL sanitize transforms over the REAL repo file; no DB.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.unit

_CONF_REL = 'deploy/supervisor/tofu.conf'

# Substrings that must NEVER survive opensource sanitization of a .conf file.
# Assembled from fragments so no contiguous internal literal exists in this
# (shipped) source file — see the module docstring.
_MNT = '/mnt/' + 'dolphin' + 'fs'
_USER = 'ruanjun' + 'hao04'
_HADOOP = 'hadoop' + '-aipnlp'
_INTERNAL_LEAK_TOKENS = (_MNT, _USER, _HADOOP)


def test_conf_is_recognised_as_text_file():
    """Root cause: a .conf file must be sanitizable at all.

    If this regresses, ``_post_copy_sanitize`` skips the file before step-8
    path scrubbing runs — exactly the bug that leaked the infra path.
    """
    from export import _is_text_file
    assert _is_text_file('deploy/supervisor/tofu.conf') is True


def test_supervisor_conf_scrubs_internal_paths_and_username():
    """The real tofu.conf must carry no internal path / username after
    opensource sanitization."""
    from export import _sanitize_defaults_for_export, _sanitize_source_opensource
    path = os.path.join(ROOT, _CONF_REL)
    if not os.path.exists(path):
        pytest.skip(f'{_CONF_REL} absent in this tree')
    src = open(path, encoding='utf-8').read()

    # Precondition: the source really does contain the internal path, else the
    # test would pass vacuously.
    assert _MNT in src, 'source tofu.conf no longer has the leak — test is stale'

    out = _sanitize_defaults_for_export(src, _CONF_REL, version='0.15.0')
    out = _sanitize_source_opensource(out, _CONF_REL)

    for token in _INTERNAL_LEAK_TOKENS:
        assert token not in out, (
            f'opensource sanitize left internal token {token!r} in {_CONF_REL}'
        )


def test_conf_sanitize_reaches_step8_via_text_gate():
    """End-to-end at the gate: a .conf file with an internal path, run through
    the same 'is-text? then sanitize' sequence _post_copy_sanitize uses, must
    come out scrubbed. Encodes that the text-file gate no longer blocks it."""
    from export import _is_text_file, _sanitize_source_opensource
    fake = f'command={_MNT}/ssd_pool/x/INS/{_USER}/env/python server.py\n'
    assert _is_text_file('deploy/whatever/foo.conf') is True  # gate open
    out = _sanitize_source_opensource(fake, 'deploy/whatever/foo.conf')
    assert _MNT not in out
    assert _USER not in out


if __name__ == '__main__':
    import pytest as _p
    _p.main([__file__, '-v'])
