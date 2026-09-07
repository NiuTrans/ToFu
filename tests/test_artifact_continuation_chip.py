"""Compacted-result read-back rows render an origin chip, not stacked verbs.

A ``read_tool_artifact`` / ``search_tool_artifact`` round continues a PRIOR
round's spilled result. The flat label (``Read compacted result of R54 ·
Read 1 file: panel.ts``) stacks two "Read" verbs and reads as a fresh
read_files row. ``_renderUnifiedToolLine`` re-composes the title as an
origin chip (``R54 compacted``) followed by the source call's own label:

1. structured ``_artifactOrigin`` (attached at round-build time) is the
   authority;
2. recovery-rebuilt / pre-meta history rounds carry only the flat query —
   a regex fallback chip-ifies those too;
3. a non-artifact tool whose query happens to match the label shape never
   gets the chip (toolName gate).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
HERE = Path(__file__).resolve().parent
TOOL_ROUNDS = Path(runtime_section_path('ui/tool_rounds.js'))
HARNESS = HERE / '_tool_rounds_wire_parity_harness.js'

SOURCE = 'Read 1 file: panel.ts'
FLAT_READ = f'Read compacted result of R54 · {SOURCE}'
FLAT_SEARCH = 'Search compacted result of R10 · web_search: citadel: download url'


def _render(rounds: list[dict], tmp_path: Path) -> list[str]:
    if shutil.which('node') is None:
        pytest.skip('node is required for the tool-round renderer')
    fixture = tmp_path / 'rounds.json'
    fixture.write_text(json.dumps(rounds), encoding='utf-8')
    process = subprocess.run(
        ['node', str(HARNESS), str(TOOL_ROUNDS), str(fixture)],
        capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return [row['html'] for row in json.loads(process.stdout)]


def test_structured_origin_renders_chip_before_source_label(tmp_path: Path):
    rounds = [{
        '_name': 'structured-read',
        'status': 'done',
        'toolName': 'read_tool_artifact',
        'query': FLAT_READ,
        '_artifactOrigin': {
            'kind': 'read', 'sourceRound': 54, 'source': SOURCE,
        },
        'results': [],
        'roundNum': 55,
    }, {
        '_name': 'structured-search',
        'status': 'done',
        'toolName': 'search_tool_artifact',
        'query': FLAT_SEARCH,
        '_artifactOrigin': {
            'kind': 'search', 'sourceRound': 10,
            'source': 'web_search: citadel', 'query': 'download url',
            'queries': ['download url'],
        },
        'results': [],
        'roundNum': 56,
    }]
    read_html, search_html = _render(rounds, tmp_path)

    assert 'ptool-badge-artifact' in read_html
    assert 'R54 compacted' in read_html
    assert SOURCE in read_html
    # The two-verb stack is gone from the row.
    assert 'Read compacted result of' not in read_html

    assert 'ptool-badge-artifact' in search_html
    assert 'R10 compacted' in search_html
    assert 'web_search: citadel' in search_html
    # The actual pattern is visible as a query chip, not just the source.
    assert 'ptool-artifact-query' in search_html
    assert '>download url</code>' in search_html


def test_search_patterns_render_chips_with_overflow_and_escaping(tmp_path: Path):
    """Batch patterns render as capped chips; overflow collapses into +N
    with the rest on hover, and patterns are HTML-escaped."""
    rounds = [{
        '_name': 'batch-search',
        'status': 'done',
        'toolName': 'search_tool_artifact',
        'query': ('Search compacted result of R5 · 2 searches: R2E-Gym: '
                  'procedural generation · hybrid environments'),
        '_artifactOrigin': {
            'kind': 'search', 'sourceRound': 5,
            'source': '2 searches: R2E-Gym',
            'query': 'procedural generation',
            'queries': [
                'procedural generation',
                'hybrid environments',
                '<img src=x onerror=alert(1)>',
                'fourth pattern',
                'fifth pattern',
            ],
        },
        'results': [],
        'roundNum': 6,
    }]
    (html,) = _render(rounds, tmp_path)

    assert html.count('ptool-artifact-query') >= 4  # 3 chips + the +N chip
    assert '>procedural generation</code>' in html
    assert '>hybrid environments</code>' in html
    # Injection-shaped pattern is escaped, never raw markup.
    assert '<img src=x onerror=alert(1)>' not in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html
    # Overflow beyond three collapses into +N listing the rest on hover.
    assert '>+2</code>' in html
    assert 'fourth pattern' in html and 'fifth pattern' in html


def test_flat_label_fallback_and_toolname_gate(tmp_path: Path):
    rounds = [{
        '_name': 'legacy-flat-read',
        'status': 'done',
        'toolName': 'read_tool_artifact',
        'query': FLAT_READ,
        'results': [],
        'roundNum': 55,
    }, {
        '_name': 'no-provenance-generic-label',
        'status': 'done',
        'toolName': 'read_tool_artifact',
        'query': 'Read saved tool result',
        'results': [],
        'roundNum': 56,
    }, {
        '_name': 'other-tool-matching-text',
        'status': 'done',
        'toolName': 'read_files',
        'query': FLAT_READ,
        'results': [],
        'roundNum': 57,
    }]
    legacy_html, generic_html, other_html = _render(rounds, tmp_path)

    # Recovery/history rounds lost the meta — the flat label is parsed.
    assert 'ptool-badge-artifact' in legacy_html
    assert 'R54 compacted' in legacy_html
    assert SOURCE in legacy_html

    # The no-provenance generic label has no source round: no chip, unchanged.
    assert 'ptool-badge-artifact' not in generic_html
    assert 'Read saved tool result' in generic_html

    # A different tool with lookalike text is never re-labeled.
    assert 'ptool-badge-artifact' not in other_html
    assert FLAT_READ in other_html
