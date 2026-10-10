"""Build-bound SyncTeX normalization and forward location in PDF points.

The layout parser remains the shared geometric decoder. Published source labels
are logical manifest paths, so queries never depend on deleted staging paths
or mistake identically named package files for a user's source file.
"""
from __future__ import annotations

import gzip
import os
from pathlib import Path

from .contract import limit
from .errors import BuildError


def _lines(stream):
    remaining = limit('synctex_decoded_bytes')
    while remaining >= 0:
        line = stream.readline(remaining + 1)
        if not line:
            return
        remaining -= len(line)
        if remaining < 0:
            raise BuildError('synctex_limit', 'SyncTeX exceeds the decoded byte limit')
        yield line


def normalize(source: Path, destination: Path, *, source_root: Path,
              source_files: tuple[dict, ...], working_directory: Path | None = None) -> None:
    known = {row['path'] for row in source_files}
    try:
        with gzip.open(source, 'rb') as incoming, gzip.open(destination, 'wb') as outgoing:
            for raw in _lines(incoming):
                if raw.startswith(b'Input:'):
                    _, tag, label = raw.decode('utf-8', errors='replace').rstrip('\r\n').split(':', 2)
                    path = Path(label)
                    if not path.is_absolute():
                        path = (working_directory or source_root) / path
                    try:
                        relative = path.resolve().relative_to(source_root.resolve()).as_posix()
                    except ValueError:
                        relative = ''
                    normalized = relative if relative in known else f'@toolchain/{tag}'
                    raw = f'Input:{tag}:{normalized}\n'.encode()
                outgoing.write(raw)
    except (OSError, EOFError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        raise BuildError('synctex_invalid', 'Cannot decode build SyncTeX') from exc


def locate(path: Path, *, build_id: str, file: str, line: int) -> dict:
    if not path.is_file():
        return _location_result(build_id, file, line)
    with path.open('rb') as stream:
        return locate_stream(stream, build_id=build_id, file=file, line=line)


def _location_result(build_id, file, line):
    from .contract import validate
    return validate({'build_id': build_id, 'file': file, 'line': line, 'page': None, 'rects': []}, 'PaperBuildLocationV1')


def _decode(source) -> str:
    try:
        with gzip.GzipFile(fileobj=source, mode='rb') as stream:
            return b''.join(_lines(stream)).decode('utf-8', errors='replace')
    except (OSError, EOFError) as exc:
        raise BuildError('synctex_invalid', 'Cannot decode build SyncTeX') from exc


def locate_stream(source, *, build_id: str, file: str, line: int) -> dict:
    from lib.paper.latex_layout import parse_synctex
    from .contract import validate
    result = _location_result(build_id, file, line)
    raw = _decode(source)
    parsed = parse_synctex(raw)
    labels = {tag for tag, label in parsed['inputs'].items()
              if os.path.normpath(label).replace('\\', '/') == file}
    candidates = []
    for tag in labels:
        for source_line in parsed['lines_by_tag'].get(tag, []):
            box = (parsed['hbox_last_by_line'].get((tag, source_line))
                   or parsed['by_line'][(tag, source_line)])
            candidates.append((abs(source_line - line), -source_line, tag, box))
    if candidates:
        box = min(candidates, key=lambda entry: entry[:3])[3]
        result['page'] = box['page']
        result['rects'] = [{'page': box['page'], 'h': max(0, box['h']),
                            'v': max(0, box['v'] - box['height']),
                            'width': max(0, box['width']), 'height': max(0, box['height'])}]
    return validate(result, 'PaperBuildLocationV1')


def inverse_stream(source, *, build_id: str, page: int, h: float, v: float) -> dict:
    """Map a PDF point (bp, origin top-left) back to its nearest source line.

    Boxes carry baseline v and height, so a click is measured against each
    box's [top, bottom] × [left, right] interval; the nearest box wins, with
    vertical distance dominating (clicks between lines still resolve).
    """
    from lib.paper.latex_layout import parse_synctex
    from .contract import validate
    raw = _decode(source)
    parsed = parse_synctex(raw)
    best = None
    for (tag, source_line), box in parsed['by_line'].items():
        if box['page'] != page:
            continue
        label = parsed['inputs'].get(tag, '')
        if not label or label.startswith('@toolchain/'):
            continue
        top, bottom = box['v'] - box['height'], box['v']
        left, right = box['h'], box['h'] + box['width']
        dy = 0.0 if top <= v <= bottom else min(abs(v - top), abs(v - bottom))
        dx = 0.0 if left <= h <= right else min(abs(h - left), abs(h - right))
        key = (dy, dx, source_line)
        if best is None or key < best[0]:
            best = (key, label, source_line)
    result = {'build_id': build_id, 'page': page, 'file': '', 'line': None}
    if best is not None:
        result['file'] = best[1][:1024]
        result['line'] = best[2]
    return validate(result, 'PaperBuildInverseLocationV1')
