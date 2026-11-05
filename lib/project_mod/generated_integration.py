"""Resolve only generated-artifact conflicts during Git integration.

The callers supply a private index with merged authored sources. Regeneration
runs in a temporary checkout, never the canonical workspace. Unknown generated
outputs keep the ordinary conflict/repair path; this module is not a merge
policy for source code or a general build runner.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

I18N_OUTPUTS = (
    'frontend/src/i18n/contract.generated.ts',
    'frontend/src/i18n/languages.generated.ts',
    'frontend/src/i18n/generated',
)
RUNTIME_OUTPUT = 'frontend/src/runtime/app-runtime.js'
STYLE_OUTPUTS = ('static/styles.css', 'static/settings.css')


class GeneratedOutputError(RuntimeError):
    """A generated family could not be rebuilt from its merged sources."""


def _families(paths):
    selected = set()
    previews = []
    for path in paths:
        if path in I18N_OUTPUTS[:2] or (path.startswith(I18N_OUTPUTS[2] + '/')
                                       and path.endswith('.generated.json')):
            selected.add('i18n')
        elif path == RUNTIME_OUTPUT:
            selected.add('runtime')
        elif path in STYLE_OUTPUTS:
            selected.add('styles')
        elif (path.startswith('designs/') and '/previews/' in path
              and path.endswith('.png')):
            previews.append(path)
        else:
            return None, []
    return selected, previews


def _generator(argv, cwd, source_root):
    from lib.project_mod.activity_supervised_process import run_activity_supervised
    from lib.project_mod.workspace_runtime import prepare_workspace_runtime

    prepare_workspace_runtime(str(source_root), str(cwd), restricted=True)
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.upper().startswith('GIT_'):
            environment.pop(name)
    result = run_activity_supervised(
        argv, cwd=cwd, env=environment,
        stall_seconds=120.0)  # Liveness contract: task_delta_delivery / git_land.
    if result.returncode:
        raise GeneratedOutputError(
            f'{argv[1]} failed: {(result.stderr or result.stdout).strip()[:700]}')


def resolve_generated_conflicts(root, conflicts, task_revision, git):
    """Rebuild known outputs from the private index's merged source bytes.

    ``git(root, args, env=..., check=...)`` is the caller's existing Git port.
    The index must contain no source conflicts. The selected task's bytes win
    only for generated design preview PNGs. Returns the selected output names,
    or None when an unknown output/source conflict must retain its repair path.
    """
    root = Path(root)
    families, previews = _families(conflicts)
    if families is None or (not families and not previews):
        return None
    outputs = []
    if 'i18n' in families:
        outputs.extend(I18N_OUTPUTS[:2])
    if 'runtime' in families:
        outputs.append(RUNTIME_OUTPUT)
    if 'styles' in families:
        outputs.extend(STYLE_OUTPUTS)
    if 'i18n' in families:
        listed = git(root, ['ls-files', '-z', '--', I18N_OUTPUTS[2]], check=True).stdout
        outputs.extend(path for path in listed.split('\0')
                       if path.endswith('.generated.json'))
    # Selecting the task's output only clears the conflict stage. Known
    # families are overwritten below from the already-merged source bytes.
    entries = git(root, ['ls-tree', '-z', task_revision, '--', *conflicts], check=True).stdout
    records = []
    for entry in entries.split('\0'):
        if '\t' not in entry:
            continue
        meta, path = entry.split('\t', 1)
        mode, kind, oid = meta.split()
        if kind != 'blob' or mode not in {'100644', '100755', '120000'}:
            raise GeneratedOutputError(f'Unsupported generated entry: {path}')
        records.append(f'{mode} {oid}\t{path}\0')
    if len(records) != len(set(conflicts)):
        raise GeneratedOutputError('Task generated-output selection is incomplete')
    git(root, ['update-index', '-z', '--index-info'],
        stdin=''.join(records), check=True)
    with tempfile.TemporaryDirectory(prefix='tofu-generated-merge-') as directory:
        merged = Path(directory)
        git(root, ['checkout-index', '--all', '--prefix', str(merged) + os.sep], check=True)
        if previews:
            archive = git(root, ['archive', task_revision, '--', *previews],
                          check=True, binary=True).stdout
            (merged / 'preview.tar').write_bytes(archive)
            subprocess.run(['tar', '-xf', str(merged / 'preview.tar')], cwd=merged,
                           check=True, capture_output=True)
            (merged / 'preview.tar').unlink()
        try:
            if 'i18n' in families:
                _generator(['node', 'scripts/gen_i18n_contract.mjs', '--write'], merged, root)
            if 'runtime' in families:
                _generator(['node', 'scripts/compose_frontend_runtime.mjs', '--write'], merged, root)
            if 'styles' in families:
                _generator(['node', 'scripts/compose_frontend_styles.mjs', '--write'], merged, root)
        except (OSError, subprocess.SubprocessError) as error:
            raise GeneratedOutputError(f'Generated-output rebuild failed: {error}') from error
        git(root, ['--work-tree', str(merged), 'add', '-A', '--', *outputs, *previews],
            check=True)
    return sorted(set(outputs) | set(previews))
