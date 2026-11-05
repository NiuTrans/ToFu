"""Snapshot an index at query execution, then replace this child with Git.

Entry point: main, invoked as ``<JSON Git -C steps> <private index> -- <Git
argv>``. The parent git_inspection_execution owner validates scope, allocates
one private destination per query, owns directory cleanup after supervised
command retirement, and executes the copy of this file it loaded at import.
An argument list in any other shape comes from a caller loaded from different
Tofu code and is refused with restart guidance. This stdlib-only child adds no
lease or worker pool and inherits the command's process group and descriptors.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def copy_index_snapshot(source, private):
    """Copy one descriptor's bytes and timestamp, preserving Git's racy check."""
    try:
        stream = open(source, 'rb')
    except FileNotFoundError:
        Path(private).unlink(missing_ok=True)
        return
    with stream:
        metadata = os.fstat(stream.fileno())
        with open(private, 'wb') as destination:
            shutil.copyfileobj(stream, destination)
    os.utime(private, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))


def _parse_arguments(arguments):
    """Return (directory steps, private index, Git argv), or None for another protocol."""
    if len(arguments) < 4 or arguments[2] != '--':
        return None
    try:
        directory_steps = json.loads(arguments[0])
    except ValueError:
        return None
    if not isinstance(directory_steps, list) or not all(isinstance(step, str) for step in directory_steps):
        return None
    return directory_steps, arguments[1], arguments[3:]


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    parsed = _parse_arguments(arguments)
    if parsed is None:
        print('Git inspection helper cannot read its arguments (expected JSON Git -C '
              'steps, private index, -- and Git argv): its caller was loaded from '
              'different Tofu code; restart the Tofu server so they match.', file=sys.stderr)
        return 126
    directory_steps, private, command = parsed
    try:
        execution_cwd = os.getcwd()
        directory_arguments = []
        for step in directory_steps:
            directory_arguments.extend(['-C', step])
            if step:
                execution_cwd = os.path.realpath(os.path.join(execution_cwd, step))
        # Earlier commands may create an index or export/unset Git's environment.
        # Use Git's resolver in its actual cwd, without importing application
        # state or treating the parent's cwd/environment as the executed source.
        # Preserve every -C step so even an invalid intermediate path fails.
        resolved = subprocess.run(
            [command[0], '--no-pager', '-c', 'core.fsmonitor=false',
             *directory_arguments, 'rev-parse', '--git-path', 'index'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True)
        source = Path(execution_cwd) / os.fsdecode(resolved.stdout).rstrip('\n')
        copy_index_snapshot(source, private)
        os.execvpe(command[0], command, {**os.environ, 'GIT_INDEX_FILE': private})
    except subprocess.CalledProcessError as error:
        detail = os.fsdecode(error.stderr or b'').strip()[:400]
        print(f'Git inspection could not resolve its index: {detail}', file=sys.stderr)
        return 126
    except OSError as error:
        print(f'Git inspection could not start: {type(error).__name__}: {str(error)[:400]}', file=sys.stderr)
        return 126


if __name__ == '__main__':
    raise SystemExit(main())
