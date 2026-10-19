"""Private C-clock compilation/loading for an already-owned PostgreSQL test cluster.

Only fixtures call this helper. It creates a function in the supplied private
cluster using a library inside that fixture's directory; no installation path,
existing service or production schema is touched. Optional source overrides are
separate test-only wrappers, never runtime clock overrides.
"""
from pathlib import Path
import os
import shutil
import subprocess

import pytest


def compile_clock_library(output: Path, *, source: Path | None = None) -> Path:
    pg_config = shutil.which('pg_config')
    compiler = shutil.which('gcc')
    if not pg_config or not compiler:
        pytest.skip('Server-clock tests require PostgreSQL headers and a C compiler')
    headers = Path(subprocess.check_output([pg_config, '--includedir-server'], text=True).strip())
    if not (headers / 'storage/dsm_registry.h').is_file():
        pytest.skip('Server-clock module requires PostgreSQL 17+ headers')
    source = source or Path(__file__).resolve().parents[2] / 'lib/storage_authority_sql/native_worker_clock/worker_clock.c'
    arguments = [compiler, '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                 '-Wno-unused-parameter', '-D_GNU_SOURCE', f'-I{headers}']
    sysroot = headers.parents[1] / 'x86_64-conda-linux-gnu/sysroot'
    if sysroot.is_dir():
        arguments.append(f'--sysroot={sysroot}')
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([*arguments, str(source), '-o', str(output)],
        capture_output=True, text=True, timeout=30,
        env={'PATH': os.environ.get('PATH', ''), 'LANG': 'C', 'LC_ALL': 'C'})
    if result.returncode:
        raise RuntimeError('Private server clock build failed: ' + result.stderr[-2000:])
    return output


def install_clock_function(dsn, library: Path, *, name='tofu_worker_lease_clock_v1'):
    import psycopg
    from psycopg import sql
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL(
            'CREATE FUNCTION public.{}() RETURNS jsonb '
            'AS {}, {} LANGUAGE C VOLATILE PARALLEL RESTRICTED'
        ).format(sql.Identifier(name), sql.Literal(str(library)),
                 sql.Literal('tofu_worker_lease_clock_v1')))


def install_worker_clock(dsn, directory: Path) -> Path:
    library = compile_clock_library(directory / 'tofu_worker_clock.so')
    install_clock_function(dsn, library)
    return library
