"""Local process-incarnation evidence shared by workflow recovery policies.

process_owner stamps host, OS boot, PID namespace, PID and kernel start time.
owner_state proves alive/dead only within the same observable namespace; missing
or inaccessible evidence is unknown. This grants no task or external-job authority.
"""
from pathlib import Path
import os
import socket

def _process_stat(pid):
    # comm may contain spaces or parentheses; fields after its final ')' start
    # with field 3. num_threads is field 20; starttime is field 22.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    state, started, threads = fields[0], fields[19], fields[17]
    if (state not in {'R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I'}
            or not started.isascii() or not started.isdecimal()
            or not threads.isascii() or not threads.isdecimal() or int(threads) < 1):
        raise ValueError('Invalid local process state or start time')
    return started, state, int(threads)


def _started(pid):
    return _process_stat(pid)[0]


def process_owner():
    try:
        return {'host': socket.gethostname(), 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'pid_namespace': str(os.stat('/proc/self/ns/pid').st_ino),
                'pid': os.getpid(), 'started': _started(os.getpid())}
    except (OSError, ValueError, IndexError):
        return None


def owner_state(owner):
    current = process_owner()
    if (not isinstance(owner, dict) or not current
            or any(owner.get(key) != current[key] for key in ('host', 'boot', 'pid_namespace'))
            or type(owner.get('pid')) is not int or owner['pid'] < 1
            or not isinstance(owner.get('started'), str)
            or not owner['started'].isascii() or not owner['started'].isdecimal()):
        return 'unknown'
    try:
        started, state, threads = _process_stat(owner['pid'])
        # An exited leader can retain living worker threads. Only its final
        # unreaped thread proves whole-process exit. Other states remain alive.
        return 'dead' if (started != owner['started']
                          or (state in {'Z', 'X', 'x'} and threads == 1)) else 'alive'
    except FileNotFoundError:
        return 'dead'
    except (OSError, ValueError, IndexError):
        return 'unknown'


__all__ = ["process_owner", "owner_state"]
