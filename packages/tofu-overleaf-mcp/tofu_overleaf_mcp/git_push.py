"""Push one frozen commit with bounded native process lifetime and no retained log.

Git's exit status owns acknowledgement. GitPython's PushInfo parser discards
successful raw-object refspecs, so it cannot attest this exact-commit operation.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from git.exc import GitCommandError


def push_commit(repo, commit, target, state, *, limits):
    state.check()
    command = [repo.git.GIT_PYTHON_GIT_EXECUTABLE, '-C', repo.working_tree_dir,
               '-c', 'pack.threads=1',
               '-c', f'pack.windowMemory={limits["overleaf_pack_window_bytes"]}',
               '-c', f'pack.deltaCacheSize={limits["overleaf_pack_delta_bytes"]}',
               'push', '--porcelain', '--', 'origin', f'{commit}:{target}']
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        start_new_session=os.name != 'nt' and not state.process_group_owned)
    try:
        while process.poll() is None:
            state.check()
            try:
                process.wait(timeout=min(0.05, max(0.001, state.deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            # Do not expose remote URLs or credential-helper diagnostics.
            raise GitCommandError('git push', process.returncode, stderr='Overleaf push was not confirmed')
    finally:
        if process.poll() is None:
            if os.name != 'nt' and not state.process_group_owned:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                import psutil
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            process.wait(timeout=5)
