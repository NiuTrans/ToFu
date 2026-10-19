#!/usr/bin/env python3
"""Run the selected Python with an explicit private Linux GLIBC runtime.

Entry: CLI. Only this process's loader/library search changes; no package,
interpreter, system library, backend or application lifecycle is mutated.
The operator supplies an already-installed, trusted runtime directory.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def command(runtime: Path, python: Path, arguments: list[str]) -> list[str]:
    for library in (runtime / 'lib/x86_64-linux-gnu', runtime / 'usr/lib/x86_64-linux-gnu',
                    runtime / 'lib64', runtime / 'lib'):
        loader = library / 'ld-linux-x86-64.so.2'
        if loader.is_file() and (library / 'libc.so.6').is_file():
            if not python.is_file():
                raise ValueError(f'Python executable not found: {python}')
            search = os.pathsep.join((str(library.resolve()),
                str(python.resolve().parent.parent / 'lib'), '/lib64', '/usr/lib64'))
            return [str(loader.resolve()), '--library-path', search, str(python.absolute()), *arguments]
    raise ValueError(f'No matched x86-64 GLIBC loader/libc found in {runtime}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(__file__).resolve().parents[1] / '.venv/bin/python')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Private GLIBC execution requires Linux')
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    try:
        argv = command(args.runtime, args.python, arguments)
    except ValueError as error:
        parser.error(str(error))
    os.execv(argv[0], argv)


if __name__ == '__main__':
    main()
