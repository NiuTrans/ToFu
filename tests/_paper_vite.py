"""Small esbuild bridge for Paper tests that execute native owners."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
ESBUILD = ROOT / 'node_modules' / '.bin' / 'esbuild'


@contextmanager
def compiled_typescript(
    source: str | os.PathLike[str],
    *,
    contents: str | None = None,
) -> Iterator[str]:
    """Bundle one browser owner to a disposable IIFE and yield its path."""
    source_path = Path(source)
    temporary_source: str | None = None
    if contents is not None:
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.ts', prefix='.paper-test-',
            dir=source_path.parent, delete=False, encoding='utf-8',
        ) as source_handle:
            source_handle.write(contents)
            temporary_source = source_handle.name
        source_path = Path(temporary_source)
    with tempfile.NamedTemporaryFile(suffix='.js', delete=False) as handle:
        output = handle.name
    try:
        proc = subprocess.run(
            [str(ESBUILD), os.fspath(source_path), '--bundle', '--format=iife',
             '--platform=browser', f'--outfile={output}'],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr or proc.stdout)
        yield output
    finally:
        try:
            os.unlink(output)
        except OSError:
            pass
        if temporary_source is not None:
            try:
                os.unlink(temporary_source)
            except OSError:
                pass
