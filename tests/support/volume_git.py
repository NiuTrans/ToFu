"""A fake ``git`` standing in for a slow or an unresponsive network volume.

The first call of one subcommand either keeps executing for ``seconds`` before
running the real Git (``slow``: a whole-tree command on a large folder) or
blocks forever in one request that never completes (``hung``: an unresponsive
volume). Every other call runs the real Git at once. Global options (``-c``,
``--git-dir``, ``--work-tree`` with their values) are skipped to find the
subcommand. The returned marker exists once that call began; for ``hung`` it
holds the blocked process id so a test can prove its retirement.
"""
import shutil
import sys


def volume_git(tmp_path, mode, subcommand, seconds=0.0):
    """Return ``(script, marker)`` for a ``slow`` or ``hung`` first ``subcommand``."""
    if mode not in ('slow', 'hung'):
        raise ValueError(mode)
    real = shutil.which('git')
    slow = tmp_path / 'slow_volume.py'
    slow.write_text(f'''import os, sys, time
deadline = time.monotonic() + {seconds!r}
while time.monotonic() < deadline:
    time.sleep(0.02)
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
''')
    marker = tmp_path / f'{mode}-{subcommand}.pid'
    delay = ('exec sleep 1000000' if mode == 'hung'
             else f'exec "{sys.executable}" "{slow}" "$@"')
    script = tmp_path / f'git-{mode}-{subcommand}'
    script.write_text(f'''#!/bin/sh
skip=
for arg in "$@"; do
  if [ -n "$skip" ]; then skip=; continue; fi
  case "$arg" in -c|--git-dir|--work-tree) skip=1; continue;; esac
  if [ "$arg" = "{subcommand}" ] && [ ! -e "{marker}" ]; then
    echo $$ > "{marker}"
    {delay}
  fi
  break
done
exec "{real}" "$@"
''')
    script.chmod(0o755)
    return script, marker
