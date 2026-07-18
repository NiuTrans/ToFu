"""The primary-source ``Authority:`` tag must survive the oversized-web_search
disk offload.

Root cause (see JOURNAL 2026-07-07): ``format_search_for_tool_response`` (in
tofu-search) emits a per-result ``Authority: …`` line so the model can tell a
vendor's OWN page from an SEO aggregator. But when a batch ``web_search`` result
exceeds budget, ``_persist_web_search_split`` rebuilds a COMPACT index (title /
URL / Status / File) — and it originally parsed only ``URL:``, silently DROPPING
the ``Authority:`` line. So on exactly the big multi-source searches where the
signal matters most (e.g. the B2 CDN pricing item, 43 results / 480 KB), the
model never saw which source was primary and synthesized wrong numbers from an
aggregator.

The fix parses ``Authority:`` like ``URL:`` and carries it into the index block.

Test plan (project double-neuter convention):
  1. Oversized web_search content whose result blocks carry ``Authority:`` →
     the returned index PRESERVES the Authority line for each such result.
  2. Content with NO Authority lines is unaffected (no spurious lines added).
  3. NEUTER: strip the Authority-carry lines from the on-disk source → the index
     LOSES the tag (proving it is load-bearing); restore byte-identical.

Run:  pytest tests/test_persist_authority_tag.py -v
"""
from __future__ import annotations

import glob
import os
import re
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PERSIST_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'lib', 'tasks_pkg', 'compaction', '_persist.py',
)

_SEP = '════════════════════'


def _result_with_authority(n: int, title: str, url: str, authority: str,
                           fill_kb: int = 40) -> str:
    body = ('lorem ipsum dolor sit amet ' * 40 + '\n') * (fill_kb * 25)
    auth = f'    Authority: {authority}\n' if authority else ''
    return (
        f'[{n}] {title}\n'
        f'    URL: {url}\n'
        f'{auth}'
        f'    ──── Full Page Content ────\n'
        f'{body}'
    )


def _make_content(with_authority: bool) -> str:
    blocks = [
        _result_with_authority(
            1, 'AWS CloudFront Pricing', 'https://aws.amazon.com/cloudfront/pricing/',
            'OFFICIAL SOURCE (vendor/authoritative domain named in query)' if with_authority else ''),
        _result_with_authority(
            2, 'CDN comparison', 'https://latencycost.com/cdn-comparison',
            'third-party aggregator — verify against a primary source' if with_authority else ''),
        _result_with_authority(
            3, 'Some blog', 'https://randomblog.example/cdn', ''),
    ]
    return _SEP.join(blocks)


def _run_split(content: str):
    from lib.tasks_pkg.compaction._persist import _persist_web_search_split
    with tempfile.TemporaryDirectory() as d:
        safe_id = uuid.uuid4().hex[:8]
        index = _persist_web_search_split(content, d, safe_id)
        files = {os.path.basename(p): open(p, encoding='utf-8').read()
                 for p in glob.glob(os.path.join(d, 'search_*.txt'))}
        return index, files


@pytest.mark.unit
class TestAuthorityTagSurvivesSplit:

    def test_authority_preserved_in_index(self):
        content = _make_content(with_authority=True)
        assert content.count(_SEP) >= 1
        index, files = _run_split(content)
        assert index is not None, 'should have split into per-result files'
        # Both non-neutral tags survive into the LLM-facing index.
        assert 'Authority: OFFICIAL SOURCE' in index
        assert 'third-party aggregator' in index
        # The official tag is attached to the AWS result, not the aggregator.
        aws_line = next(ln for ln in index.splitlines()
                        if 'aws.amazon.com/cloudfront/pricing' in ln)
        # index blocks are multi-line; assert co-location by section slicing
        aws_idx = index.index('aws.amazon.com/cloudfront/pricing')
        agg_idx = index.index('latencycost.com')
        assert 'OFFICIAL SOURCE' in index[aws_idx:agg_idx]
        assert aws_line  # sanity

    def test_no_authority_lines_when_absent(self):
        content = _make_content(with_authority=False)
        index, files = _run_split(content)
        assert index is not None
        assert 'Authority:' not in index

    def test_neuter_proves_carry_is_load_bearing(self):
        """In-memory neuter (xdist-safe): strip the Authority-carry line via the
        shared NC harness → the tag disappears from the index (proving it is
        load-bearing). The shipped source is opened read-only, never written."""
        from tests._nc_harness import neutered_source
        needle = "        _auth_line = f'\\n    Authority: {authority}' if authority else ''\n"
        with neutered_source(_PERSIST_SRC, needle,
                             "        _auth_line = ''  # NEUTERED\n") as _p:
            content = _make_content(with_authority=True)
            with tempfile.TemporaryDirectory() as d:
                idx = _p._persist_web_search_split(content, d, uuid.uuid4().hex[:8])
            assert idx is not None
            assert 'Authority:' not in idx, 'neuter did not drop the tag'
