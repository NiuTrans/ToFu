"""Compile one frozen paper source tree and produce an immutable receipt.

Entry point: compile_snapshot. Transport/task adapters own admission, owner
authorization and cancellation events; this module owns the subprocess and all
of its outputs. No subprocess result is inferred from a pre-existing PDF.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import limit, validate
from .errors import BuildError, check_cancelled
from .sources import SourceSnapshot
from .toolchain import prepare_toolchain
from .pdf_validation import validate_pdf
from .process import run_command


@dataclass(frozen=True)
class CompiledBuild:
    receipt: dict
    directory: Path


def _output_bytes(directory: Path) -> int:
    size = count = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    count += 1
                    if count > limit('scan_entries'):
                        raise BuildError('output_limit', 'Paper build produced too many output files')
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # TeX and private worker IPC can retire temporary files.
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        size += info.st_size
                    else:
                        raise BuildError('output_path', 'Paper build output must be a regular file or directory')
                    if size > limit('build_output_bytes'):
                        raise BuildError('output_limit', 'Paper build exceeds the output byte limit')
        except FileNotFoundError:
            continue
    return size


# -file-line-error emits every engine error as ``file:line: message``; classic
# transcript lines (``! …`` / ``Warning:``) carry their position in a trailing
# ``on input line N`` or a following ``l.N`` record instead.
_FILE_LINE = re.compile(
    r'^(?P<file>(?:\.{1,2}[/\\]|/|[A-Za-z]:[\\/])[^\n:()]+?):(?P<line>[1-9][0-9]*):(?P<message>.*)$')
_ON_INPUT_LINE = re.compile(r'\bon input line ([1-9][0-9]*)\b')
_LOG_LINE = re.compile(r'^l\.([1-9][0-9]*)\b')
_MISSING_FILE = re.compile(r"File `([^']+)' not found")
_CITATION_UNDEFINED = re.compile(r"Citation `([^']+)' .*undefined")
_REFERENCE_UNDEFINED = re.compile(r"Reference `([^']+)' .*undefined")


def _diagnostic_file(raw: str, *, source_root: Path | None, base_dir: Path | None) -> str:
    """Publish source-tree-relative labels so callers never see staging paths."""
    candidate = Path(raw.strip())
    try:
        if not candidate.is_absolute() and base_dir is not None:
            candidate = base_dir / candidate
        if source_root is not None:
            return candidate.resolve().relative_to(source_root.resolve()).as_posix()[:512]
    except (OSError, ValueError):
        pass
    raw = raw.strip()
    return (raw[2:] if raw.startswith('./') else raw)[:512]


def _diagnostic_code(severity: str, message: str) -> str:
    if severity == 'warning':
        if _CITATION_UNDEFINED.search(message):
            return 'undefined_citation'
        if _REFERENCE_UNDEFINED.search(message):
            return 'undefined_reference'
        return 'tex_warning'
    if _MISSING_FILE.search(message):
        return 'missing_package'
    return 'tex_error'


def _diagnostics(text: str, *, source_root: Path | None = None,
                 base_dir: Path | None = None) -> list[dict]:
    rows = []
    seen = set()
    current_file = ''
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        located = _FILE_LINE.match(line)
        if located:
            # With -file-line-error every prefixed record is an engine error;
            # warnings only carry the prefix when a package opts in.
            message = located.group('message').strip()[:2000]
            current_file = _diagnostic_file(located.group('file'),
                                            source_root=source_root, base_dir=base_dir)
            severity = 'warning' if 'Warning' in message else 'error'
            row = {'severity': severity, 'code': _diagnostic_code(severity, message),
                   'message': message, 'file': current_file,
                   'line': int(located.group('line'))}
        elif line.startswith('!') or 'LaTeX Error:' in line or 'Undefined control sequence' in line:
            message = line[:2000]
            line_no = next((int(match.group(1)) for follow in lines[index + 1:index + 8]
                            if (match := _LOG_LINE.match(follow.strip()))), None)
            row = {'severity': 'error', 'code': _diagnostic_code('error', message),
                   'message': message, 'file': current_file, 'line': line_no}
        elif 'Warning:' in line or 'undefined references' in line:
            message = line[:2000]
            on_input = _ON_INPUT_LINE.search(line)
            row = {'severity': 'warning', 'code': _diagnostic_code('warning', message),
                   'message': message, 'file': current_file,
                   'line': int(on_input.group(1)) if on_input else None}
        else:
            continue
        key = (row['severity'], row['file'], row['line'], row['message'][:200])
        if key in seen:
            continue  # latexmk transcripts repeat identical records across passes
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit('diagnostics'):
            break
    return rows


def _tree_bytes(path: Path) -> int:
    try:
        return _output_bytes(path)
    except (BuildError, OSError):
        return limit('build_output_bytes') + 1


def artifact_metadata(path: Path) -> dict:
    size = path.stat().st_size
    if not 0 < size <= limit('artifact_bytes') or path.is_symlink():
        raise BuildError('output_limit', 'Paper artifact exceeds its byte limit')
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk)
    return {'name': path.name, 'sha256': sha.hexdigest(), 'size_bytes': size}


def compile_snapshot(snapshot: SourceSnapshot, directory: Path, *, owner_user_id: int,
                     document_id: str, build_id: str, engine: str = 'auto',
                     target: str = 'host', cancel=None,
                     timeout: float | None = None,
                     warm_dir: Path | None = None,
                     toolchain_state: Path | None = None) -> CompiledBuild:
    """Return a terminal build receipt. Failed work never contains a public PDF.

    directory must be fresh and external to the frozen source root. The worker
    kills its process group on cancellation, timeout or output pressure. Logs
    use disk rather than an unbounded PIPE and are checked throughout execution.
    """
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id < 1:
        raise BuildError('owner', 'Paper build requires an explicit owner')
    directory.mkdir(parents=True, exist_ok=False)
    started = int(time.time() * 1000)
    receipt = {
        'schema_version': 1, 'build_id': build_id, 'document_id': document_id,
        'owner_user_id': owner_user_id, 'target': target,
        'source_digest': snapshot.digest, 'version_verified': True,
        'main_file': snapshot.main_file, 'engine': 'pdflatex' if engine == 'auto' else engine,
        'toolchain': '', 'status': 'failed', 'started_at': started, 'completed_at': started,
        'artifacts': [], 'diagnostics': [],
    }
    validate(receipt)
    logfile = directory / 'build.log'
    try:
        check_cancelled(cancel)
        executable, selected, version, env = prepare_toolchain(
            snapshot.root, snapshot.main_file, directory / 'tool-state', engine, cancel,
            shared_state=toolchain_state)
        receipt.update(engine=selected, toolchain=version)
        out = directory / 'output'
        using_warm = False
        if warm_dir is not None:
            # A stable per-document output path keeps latexmk's dependency
            # database valid across builds: unchanged sources compile to
            # "up-to-date" in zero engine passes, changed ones in the minimum.
            try:
                candidate = warm_dir / 'output'
                candidate.mkdir(parents=True, exist_ok=True)
                if _tree_bytes(candidate) > limit('warm_state_bytes'):
                    shutil.rmtree(candidate)
                    candidate.mkdir()
                out = candidate
                using_warm = True
            except OSError:
                out = directory / 'output'
        out.mkdir(parents=True, exist_ok=True)
        main_path = snapshot.root / snapshot.main_file
        cmd = [executable, '-norc', {'pdflatex': '-pdf', 'xelatex': '-xelatex',
                                    'lualatex': '-lualatex'}[selected],
               '-no-shell-escape', '-synctex=1', '-interaction=nonstopmode',
               '-halt-on-error', '-file-line-error', f'-outdir={out}',
               './' + main_path.name]

        def attempt():
            def inspect():
                _output_bytes(directory)
                if using_warm:
                    _output_bytes(out)
            return run_command(cmd, cwd=main_path.parent, environment=env,
                log=logfile, timeout=timeout if timeout is not None else limit('compile_timeout_seconds'),
                log_bytes=limit('log_bytes'), cancel=cancel, inspect_output=inspect)

        seeded = using_warm and any(out.iterdir())
        execution = attempt()
        returncode = execution.returncode
        if returncode != 0 and seeded:
            # Warm state is only a hint. A stale or corrupt aux must never keep
            # a document broken, so retry once from a clean output tree.
            shutil.rmtree(out)
            out.mkdir()
            execution = attempt()
            returncode = execution.returncode
        check_cancelled(cancel)
        _output_bytes(directory)
        stem = main_path.stem
        # latexmk's transcript includes warnings from intermediate passes;
        # the final TeX log describes the PDF we actually publish.
        final_log = out / (stem + '.log')
        diagnostic_log = final_log if returncode == 0 and final_log.is_file() else logfile
        with diagnostic_log.open('rb') as stream:
            text = stream.read(limit('log_bytes')).decode('utf-8', errors='replace')
        diagnostics = _diagnostics(text, source_root=snapshot.root,
                                   base_dir=main_path.parent)
        try:
            from .citation_audit import audit_citations
            diagnostics = (diagnostics + audit_citations(
                snapshot.root, snapshot.files, existing=diagnostics))[:limit('diagnostics')]
        except (BuildError, OSError):
            # The audit is advisory; the TeX log diagnostics stay authoritative.
            pass
        receipt['diagnostics'] = diagnostics
        if returncode != 0 or any(d['severity'] == 'error' for d in diagnostics):
            raise BuildError('compile_failed', 'Paper compilation failed; inspect the build log')
        pdf = out / (stem + '.pdf')
        validate_pdf(pdf, cancel=cancel)
        if using_warm:
            # The warm PDF stays in place: removing latexmk's target would
            # force a full recompile of the next, possibly unchanged, build.
            shutil.copyfile(pdf, directory / 'document.pdf')
        else:
            os.replace(pdf, directory / 'document.pdf')
        synctex = out / (stem + '.synctex.gz')
        if synctex.is_file():
            from .synctex import normalize
            try:
                normalize(synctex, directory / 'document.synctex.gz',
                          source_root=snapshot.root, source_files=snapshot.files,
                          working_directory=main_path.parent)
            except BuildError as exc:
                (directory / 'document.synctex.gz').unlink(missing_ok=True)
                receipt['diagnostics'] = receipt['diagnostics'][:limit('diagnostics') - 1] + [{
                    'severity': 'warning', 'code': exc.code, 'message': str(exc),
                    'file': '', 'line': None}]
        (directory / 'sources.json').write_text(json.dumps(list(snapshot.files), separators=(',', ':')))
        check_cancelled(cancel)
        if using_warm and _tree_bytes(out) > limit('warm_state_bytes'):
            shutil.rmtree(out, ignore_errors=True)  # oversized state: next build runs cold
        receipt['status'] = 'success'
    except BuildError as exc:
        receipt['status'] = 'cancelled' if exc.code == 'cancelled' else 'failed'
        receipt['diagnostics'] = (receipt['diagnostics'][:limit('diagnostics') - 1] + [{
            'severity': 'error', 'code': exc.code, 'message': str(exc), 'file': '', 'line': None}])
    except OSError:
        receipt['diagnostics'] = [{'severity': 'error', 'code': 'compile_io',
                                   'message': 'Cannot execute or store the paper build',
                                   'file': '', 'line': None}]
    finally:
        if logfile.exists() and logfile.stat().st_size > limit('log_bytes'):
            with logfile.open('r+b') as stream:
                stream.truncate(limit('log_bytes'))
    names = ['build.log']
    if receipt['status'] == 'success':
        names += ['document.pdf', 'document.synctex.gz', 'sources.json']
    for name in names:
        path = directory / name
        if path.is_file() and path.stat().st_size:
            receipt['artifacts'].append(artifact_metadata(path))
    receipt['completed_at'] = int(time.time() * 1000)
    validate(receipt)
    (directory / 'receipt.json').write_text(json.dumps(receipt, separators=(',', ':')))
    return CompiledBuild(receipt, directory)
