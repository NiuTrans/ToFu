"""Linux counters for the current process image, without inherited rusage peaks.

process_status(pid) reads kernel procfs estimates. Callers own identity and
pause requirements; VmHWM is an RSS estimate, never a historical PSS value.
"""
from pathlib import Path


def process_status(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('positive process or thread identity required')
    values = {}
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        key, _, value = line.partition(':')
        if key in ('Tgid', 'Pid', 'PPid', 'Threads'):
            values[key] = int(value)
        elif key in ('VmHWM', 'VmRSS'):
            number, unit = value.split()
            if unit != 'kB':
                raise ValueError('unsupported process memory unit')
            values[key] = int(number) * 1024
    if not all(key in values for key in ('Tgid', 'Pid', 'PPid', 'Threads')):
        raise ValueError('incomplete process status')
    return values
