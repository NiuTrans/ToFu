"""Commit a verified installer candidate; never modify the selected env.

Entrypoints: commit(root), discard_candidates(root, candidates). Stdlib only.
Candidate paths belong to the checkout install lock. On pointer publication
failure restore configuration; cleanup protects the published interpreter.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile


def _durable_replace(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.install-commit-', dir=path.parent)
    try:
        try:
            with os.fdopen(fd, 'wb', closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(fd)
        finally:
            # The stream only borrows the descriptor, including when stream
            # construction fails. Close exactly once and before Windows rename.
            os.close(fd)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def commit(root: Path) -> None:
    root = root.resolve()
    pointer = root / '.tofu_env.json'
    candidate = root / '.tofu_env.pending.json'
    configuration = root / '.env'
    staged_configuration = root / '.tofu-env.pending'
    for path in (pointer, candidate, configuration, staged_configuration):
        if path.is_symlink():
            raise ValueError(f'Refusing symlink at installation commit: {path.name}')
    candidate_content = candidate.read_bytes()
    marker = json.loads(candidate_content)
    prefix = Path(marker['env_prefix']).resolve()
    environments = root / '.tofu-envs'
    python = Path(marker['python'])
    # Resolve the containing directory, not the executable: venvs normally
    # symlink bin/python to a managed base interpreter outside the candidate.
    if (prefix == environments or not prefix.is_relative_to(environments)
            or not prefix.is_dir() or not python.is_absolute()
            or not python.parent.resolve().is_relative_to(prefix) or not python.is_file()):
        raise ValueError('Candidate environment is missing or outside this checkout')
    previous = configuration.read_bytes() if configuration.exists() else None
    _durable_replace(configuration, staged_configuration.read_bytes())
    try:
        _durable_replace(pointer, candidate_content)
    except BaseException:
        if previous is None:
            configuration.unlink()
        else:
            _durable_replace(configuration, previous)
        raise
    candidate.unlink()
    staged_configuration.unlink()


def discard_candidates(root: Path, candidates: list[Path]) -> None:
    """Reclaim this invocation's unpublished environments under its install lock.

    Never discover/sweep old generations. An unreadable live marker prevents
    cleanup because an interruption may have happened after publication.
    """
    root = root.resolve()
    environments = root / '.tofu-envs'
    pointer = root / '.tofu_env.json'
    selected = []
    if pointer.is_symlink():
        raise ValueError('Refusing cleanup with a symlinked environment marker')
    if pointer.exists():
        marker = json.loads(pointer.read_bytes())
        if not isinstance(marker, dict):
            raise ValueError('Cannot prove selected environment ownership')
        for key in ('env_prefix', 'python'):
            value = marker.get(key)
            if value:
                path = Path(value)
                if not path.is_absolute():
                    raise ValueError('Cannot prove selected environment ownership')
                selected.append(path.resolve() if key == 'env_prefix'
                                else path.parent.resolve() / path.name)
        if not selected:
            raise ValueError('Cannot prove selected environment ownership')
    # Validate the entire input before removing anything. The shell supplies
    # exactly the mktemp paths it created, not a filesystem scan or a pattern.
    validated = []
    for candidate in candidates:
        if (candidate.is_symlink() or candidate.parent.resolve() != environments
                or candidate.name in ('', '.', '..')):
            raise ValueError('Refusing cleanup outside candidate environments')
        validated.append(candidate.parent.resolve() / candidate.name)
    for candidate in validated:
        if any(path == candidate or path.is_relative_to(candidate) for path in selected):
            continue
        if candidate.exists():
            shutil.rmtree(candidate)
