"""Every path in the READMEs' Project Structure tree must exist on disk.

Why this suite exists (measured 2026-07-31)
-------------------------------------------
The ``## Project Structure`` block is the onboarding map: README.md's
``## For AI Agents & Developers`` section points readers straight at it, and it
is the first thing both a new contributor and an AI assistant read to find
their way around. Seven of its paths named files that no longer exist:

    lib/tasks_pkg/orchestrator.py   -> orchestrator/ (package)
    lib/tasks_pkg/executor.py       -> executor/     (package)
    lib/tasks_pkg/endpoint.py       -> endpoint/     (package)
    lib/image_gen.py                -> image_gen/    (package)
    lib/mt_provider.py              -> mt_provider/  (package)
    lib/fetch/                      -> removed (external `tofu_search`)
    lib/search/                     -> removed (external `tofu_search`)

Three of those were the headline entries — "Main LLM ↔ tool loop", "Tool
execution engine", "Planner → Worker → Critic loop" — so the map sent every
new reader to files that are not there. Identical drift in BOTH language
files, because they are kept in lockstep.

Nothing could notice. The `.py` -> package promotions are invisible to any
check that greps for a name, and the two removed packages left with a
subsystem extraction months earlier. Documentation drift of this kind is only
ever caught by a human happening to click through — which is to say, not
caught.

The rule is deliberately structural, not textual: parse the fenced tree, walk
the box-drawing indentation back to a real repo-relative path, and stat it.
A guard that merely grepped for known-bad names would need updating every time
a different path rotted; this one cannot go stale, because it re-derives the
path set from the document each run.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_READMES = ('README.md', 'README_CN.md')

#: Entries that are intentionally not real paths — ellipses and prose.
_SKIP_TOKENS = {'...', '…'}


def _tree_block(text: str) -> list[str]:
    """The lines of the fenced code block under ``## Project Structure``."""
    m = re.search(r'^##\s+(?:Project Structure|项目结构).*$', text, re.MULTILINE)
    if not m:
        return []
    rest = text[m.end():]
    fence = re.search(r'^```[^\n]*\n(.*?)^```', rest, re.MULTILINE | re.DOTALL)
    if not fence:
        return []
    return fence.group(1).splitlines()


def _declared_paths(lines: list[str]) -> list[tuple[str, str]]:
    """Reconstruct ``(repo_relative_path, source_line)`` from a box-drawing tree.

    The tree nests with ``│   ``/``    `` prefixes and ``├──``/``└──`` markers,
    so a leaf's real path is its own name appended to the names of its
    ancestors. Depth is derived from the marker's column, which is what the
    renderer itself uses, rather than from a count of leading spaces.
    """
    out: list[tuple[str, str]] = []
    stack: dict[int, str] = {}
    for raw in lines:
        m = re.match(r'^(?P<prefix>[│\s]*)(?:├──|└──)\s*(?P<name>\S+)', raw)
        if not m:
            continue
        name = m.group('name')
        if name in _SKIP_TOKENS:
            continue
        depth = len(m.group('prefix'))
        # Drop any sibling/deeper frames left from the previous branch.
        for d in [d for d in stack if d >= depth]:
            del stack[d]
        parent = ''
        if stack:
            parent = stack[max(stack)]
        rel = f'{parent}{name}'
        stack[depth] = rel if rel.endswith('/') else rel + '/'
        out.append((rel.rstrip('/'), raw.rstrip()))
    return out


@pytest.mark.parametrize('readme', _READMES)
def test_every_documented_path_exists(readme):
    """THE GUARD. A path in the onboarding map must exist on disk.

    Fails today on the seven rotted entries. Because the path set is
    re-derived from the document, this keeps working for paths that rot later
    without anyone updating this test.
    """
    text = (_ROOT / readme).read_text(encoding='utf-8')
    lines = _tree_block(text)
    assert lines, f'{readme} has no fenced Project Structure tree to check'

    missing = [(p, src) for p, src in _declared_paths(lines)
               if not (_ROOT / p).exists()]
    assert not missing, (
        f'{readme} documents {len(missing)} path(s) that do not exist on disk. '
        'This block is the onboarding map (README\'s "For AI Agents & '
        'Developers" section points at it), so a wrong path sends every new '
        'reader and assistant to a file that is not there:\n'
        + '\n'.join(f'    {p}\n      from: {src.strip()}' for p, src in missing))


def test_the_tree_is_actually_being_parsed():
    """OVER-FIRING complement: the parser must find real paths, not zero.

    Without this, `test_every_documented_path_exists` is satisfiable by a
    parser that silently extracts nothing — the classic way a structural guard
    goes hollow. Pins that the extraction finds a substantial set AND that a
    known-good, definitely-present path is among them.
    """
    text = (_ROOT / 'README.md').read_text(encoding='utf-8')
    paths = [p for p, _src in _declared_paths(_tree_block(text))]
    assert len(paths) >= 25, (
        f'only {len(paths)} paths extracted from the tree — the parser is '
        f'probably not matching the box-drawing markers: {paths!r}')
    assert 'lib/llm' in paths, (
        f'expected the nested entry lib/llm to be reconstructed from its '
        f'parent; got {[p for p in paths if "llm" in p]!r}')
    assert 'server.py' in paths, 'expected the top-level server.py entry'


def test_nesting_is_resolved_against_the_parent_directory():
    """The reconstruction must be real, not a flat basename scan.

    ``compaction/`` alone is not a repo path; it is only meaningful as
    ``lib/tasks_pkg/compaction``. If the parser flattened the tree, the guard
    above would look for a top-level ``compaction`` and fail for the wrong
    reason — or, worse, a bare name that happens to exist at the root would
    make a genuinely wrong nested path pass.
    """
    text = (_ROOT / 'README.md').read_text(encoding='utf-8')
    paths = [p for p, _src in _declared_paths(_tree_block(text))]
    assert 'lib/tasks_pkg/compaction' in paths, (
        f'nested entry not resolved against its parent; got '
        f'{[p for p in paths if "compaction" in p]!r}')


def test_both_readmes_document_the_same_path_set():
    """EN and CN must stay in lockstep.

    They drifted together last time, which is the good case; this pins that
    fixing one language can never silently leave the other behind.
    """
    sets = {}
    for r in _READMES:
        text = (_ROOT / r).read_text(encoding='utf-8')
        sets[r] = {p for p, _s in _declared_paths(_tree_block(text))}
    only_en = sets['README.md'] - sets['README_CN.md']
    only_cn = sets['README_CN.md'] - sets['README.md']
    assert not only_en and not only_cn, (
        'the two READMEs document different path sets — they must be kept in '
        f'sync.\n  only in README.md: {sorted(only_en)}\n'
        f'  only in README_CN.md: {sorted(only_cn)}')
