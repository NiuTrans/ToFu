"""Portable TeX toolchain selection and per-build font/cache environment.

Entry point: prepare_toolchain. Uses an explicitly configured TeX root, an
installed PATH toolchain, or the bundled TeX tree. Never installs packages or
changes a user's font configuration. No process-global environment mutation.
Generated formats and the font cache are pure caches: when the caller supplies
shared_state they persist across builds of the toolchain, while every
document write stays confined to the per-build scratch (TEXMFOUTPUT).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path

from .errors import BuildError, check_cancelled
from .contract import limit
from .process import run_command

_UNICODE = re.compile(r'\\(?:usepackage|documentclass)\s*(?:\[[^\]]*\])?\s*'
                      r'\{[^}]*(?:fontspec|xeCJK|unicode-math|ctex)')
_PROGRAM = re.compile(r'^\s*%\s*!\s*TEX\s+program\s*=\s*(\w+)', re.I | re.M)


def toolchain_environment(workdir: Path, scratch: Path, shared_state: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    configured = environment.get('TOFU_TEX_ROOT', '').strip()
    texroot = (Path(configured).expanduser() if configured
               else Path(__file__).resolve().parents[3] / '.texlive')
    bins = texroot / 'bin'
    if bins.is_dir():
        candidates = sorted(p for p in bins.iterdir() if p.is_dir())
        executable = 'latexmk.exe' if os.name == 'nt' else 'latexmk'
        machine = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(
            platform.machine().lower(), platform.machine().lower())
        platform_name = ('windows' if os.name == 'nt' else
                         'darwin' if platform.system() == 'Darwin' else f'{machine}-linux')
        candidates = [p for p in candidates if (p / executable).is_file() and (
            platform_name in p.name or (platform_name == 'windows' and p.name == 'win32'))]
        if candidates:
            environment['PATH'] = str(candidates[0]) + os.pathsep + environment.get('PATH', '')
    syslib = texroot / 'syslib'
    if os.name == 'posix' and syslib.is_dir():
        environment['LD_LIBRARY_PATH'] = str(syslib) + os.pathsep + environment.get('LD_LIBRARY_PATH', '')
    scratch.mkdir(parents=True, exist_ok=True)
    # Generated formats and the font cache are pure caches: sharing them across
    # builds of a toolchain is standard TeX behaviour and changes no inputs.
    # Per-build writes stay confined to scratch (TEXMFOUTPUT) either way.
    state = scratch if shared_state is None else shared_state
    state.mkdir(parents=True, exist_ok=True)
    # Font aliases are a shared policy. The owning layout module imports the
    # compiler lazily; no duplicate compiler or font rule set is introduced.
    from lib.paper.latex_layout import _fonts_conf_text
    fonts = scratch / 'fonts.conf'
    fonts.write_text(_fonts_conf_text(workdir, state / 'font-cache',
                                     base_conf=environment.get('FONTCONFIG_FILE'), texroot=texroot))
    environment.update({
        'FONTCONFIG_FILE': str(fonts),
        'TEXMFVAR': str(state / 'texmf-var'),
        'TEXMFCONFIG': str(state / 'texmf-config'),
        'TEXMFOUTPUT': str(scratch),
        'openin_any': 'p', 'openout_any': 'p', 'shell_escape': 'f',
    })
    return environment


def prepare_toolchain(source_root: Path, main_file: str, scratch: Path,
                      requested_engine: str = 'auto', cancel=None,
                      shared_state: Path | None = None) -> tuple[str, str, str, dict]:
    check_cancelled(cancel)
    engine = requested_engine
    if engine == 'auto':
        text = (source_root / main_file).read_text(errors='replace')
        hint = _PROGRAM.search(text)
        engine = hint[1].lower() if hint else ('xelatex' if _UNICODE.search(text) else 'pdflatex')
    if engine not in {'pdflatex', 'xelatex', 'lualatex'}:
        raise BuildError('engine', 'Choose pdflatex, xelatex or lualatex')
    environment = toolchain_environment((source_root / main_file).parent, scratch,
                                        shared_state=shared_state)
    latexmk = shutil.which('latexmk', path=environment.get('PATH'))
    executable = shutil.which(engine, path=environment.get('PATH'))
    if not latexmk or not executable:
        raise BuildError('toolchain_missing', f'Install a TeX toolchain with latexmk and {engine}')
    version_log = scratch / 'toolchain-version.log'
    try:
        result = run_command([executable, '--version'], cwd=source_root, environment=environment,
            log=version_log, timeout=limit('toolchain_probe_seconds'),
            log_bytes=limit('toolchain_probe_bytes'),
            # A sentinel byte distinguishes a complete maximum-sized response
            # from a native short write silently truncated at RLIMIT_FSIZE.
            file_bytes=limit('toolchain_probe_bytes') + 1, cancel=cancel)
        if result.returncode != 0:
            raise BuildError('toolchain_missing', f'Cannot start {engine}')
        with version_log.open('rb') as output:
            version = output.read(limit('toolchain_probe_bytes')).decode('utf-8', errors='replace')
    except OSError as exc:
        raise BuildError('toolchain_missing', f'Cannot start {engine}') from exc
    finally:
        version_log.unlink(missing_ok=True)
    return latexmk, engine, (version.splitlines() or [engine])[0][:512], environment
