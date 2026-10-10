"""Lazy, reconstructible runtime dependencies for an owned task worktree.

Entry point: prepare_workspace_runtime(source, workspace). Python gets a
private environment prefix with the source's installed packages readable;
pip writes stay local. Node packages are shared through an ignored private directory.
The caller owns task authorization, serialization and durable path receipts.
"""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
import subprocess


def prepare_workspace_runtime(source: str, workspace: str, *, restricted: bool = False) -> list[str]:
    source_root, task_root = Path(source), Path(workspace)
    created = []
    source_env = source_root / '.venv'
    task_env = task_root / '.venv'
    python = source_env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    source_marked = (source_env / 'pyvenv.cfg').is_file()
    if source_marked and restricted and not source_env.resolve().is_relative_to(source_root.resolve()):
        raise ValueError('Project .venv escapes its authorized environment root')
    if source_marked and not task_env.exists():
        if not python.is_file() or not os.access(python, os.X_OK):
            # Preserve the unavailable Python selection in the private tree;
            # unrelated commands still run, Python cannot select server packages.
            task_env.mkdir()
            try:
                shutil.copyfile(source_env / 'pyvenv.cfg', task_env / 'pyvenv.cfg')
            except BaseException:
                shutil.rmtree(task_env)
                raise
            created.append('.venv')
        else:
            try:
                from lib.project_mod.run_command import _is_sensitive_inherited_env
                environment = {key: value for key, value in os.environ.items()
                               if not _is_sensitive_inherited_env(key)}
                environment['PYTHONNOUSERSITE'] = '1'
                for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'LD_LIBRARY_PATH'):
                    environment.pop(key, None)
                subprocess.run([str(python), '-m', 'venv', '--without-pip', str(task_env)],
                               env=environment, check=True, capture_output=True, timeout=60)
                code = 'import sysconfig; print(sysconfig.get_path("purelib"))'
                packages = subprocess.run([str(python), '-c', code], env=environment,
                                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()
                local_packages = subprocess.run(
                    [str(task_env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')), '-c', code],
                    env=environment, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
                package_root = Path(local_packages)
                if (not package_root.is_relative_to(task_env)
                        or not Path(packages).resolve().is_relative_to(source_env.resolve())):
                    raise ValueError('Workspace Python environment returned an unrelated package directory')
                (package_root / 'tofu-source-packages.pth').write_text(
                    f'import site; site.addsitedir({packages!r})\n', encoding='utf-8')
                if os.name != 'nt':
                    script = '#!/bin/sh\nexec ' + shlex.quote(str(task_env / 'bin/python')) + ' -m pip "$@"\n'
                    for name in ('pip', 'pip3'):
                        pip = task_env / 'bin' / name
                        pip.write_text(script, encoding='utf-8')
                        pip.chmod(0o700)
                created.append('.venv')
            except BaseException:
                if task_env.exists():
                    shutil.rmtree(task_env)
                raise
    dependencies = source_root / 'node_modules'
    destination = task_root / 'node_modules'
    if dependencies.is_dir() and not destination.exists():
        # Keep the directory real: conventional node_modules/ ignore rules
        # do not match a root-level symlink. Only installed package entries
        # are links; task-local installs can replace them without replacing
        # the source project's node_modules directory.
        destination.mkdir()
        for entry in dependencies.iterdir():
            (destination / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        created.append('node_modules')
    return created
