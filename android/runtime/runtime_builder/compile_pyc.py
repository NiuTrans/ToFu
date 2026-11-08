"""Compile every .py under the given roots to unchecked-hash bytecode.

Run by the build host's python-build-standalone interpreter, which has the
payload's CPython version, so the bytecode matches the target's. Usage::

    python compile_pyc.py <stage> <guest-root> <relative-root>...

Files are compiled sequentially in sorted order in this one process with
PYTHONHASHSEED=0 (set by the caller): marshal output depends on interpreter
state, so a fixed order keeps the .pyc bytes reproducible. ``dfile`` records
the guest path (``<guest-root>/<relative path>``), never the staging path.
Prints a JSON list of files that do not compile; they ship without .pyc.
"""
import json
import os
import py_compile
import sys


def main() -> int:
    stage, guest_root, *roots = sys.argv[1:]
    files = []
    for root in roots:
        for directory, dirnames, filenames in os.walk(os.path.join(stage, root)):
            dirnames[:] = sorted(name for name in dirnames if name != '__pycache__')
            files.extend(os.path.join(directory, name) for name in filenames if name.endswith('.py'))
    files.sort(key=lambda path: os.path.relpath(path, stage).encode('utf-8'))
    failures = []
    for path in files:
        relative = os.path.relpath(path, stage).replace(os.sep, '/')
        try:
            py_compile.compile(
                path, dfile=f'{guest_root}/{relative}', doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        except (py_compile.PyCompileError, SyntaxError, ValueError):
            failures.append(relative)
    json.dump(failures, sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
