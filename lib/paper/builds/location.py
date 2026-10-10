"""Resolve edit hints against a frozen build; shared by host and device workers."""
from pathlib import Path

from .sources import read_source
from .synctex import locate


def needle_to_line(text: str, needle: str) -> int | None:
    if not needle:
        return None
    index = text.find(needle)
    if index >= 0 and text.find(needle, index + len(needle)) == -1:
        return text.count('\n', 0, index) + 1
    first = next((line.strip() for line in needle.splitlines() if line.strip()), '')
    matches = [number for number, line in enumerate(text.splitlines(), 1)
               if first and line.strip() == first]
    return matches[0] if len(matches) == 1 else None


def build_location(build, snapshot, edited_file: str, *, line=None, needle=''):
    if build.receipt['status'] != 'success' or Path(edited_file).suffix.lower() not in {'.tex', '.ltx'}:
        return None
    if line is None and needle:
        source = read_source(snapshot.root, edited_file).decode('utf-8', errors='replace')
        line = needle_to_line(source, needle)
    if line is None:
        return None
    return locate(build.directory / 'document.synctex.gz',
                  build_id=build.receipt['build_id'], file=edited_file, line=line)
