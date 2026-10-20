"""Source-hint uncertainty survives oversized web-search result persistence.

The compact index retains each result's complete Authority field, including
the qualifier that URL-derived hints are unverified. It neither invents tags
for neutral results nor upgrades a spelling/host heuristic to verified source
identity. An in-memory fault injection verifies the carry without source edits.
"""
from __future__ import annotations

import glob
import os
import re
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_PERSIST_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    # _persist became a PACKAGE; the Authority-carry line the neuter strips
    # lives in its _splitters submodule.
    'lib', 'tasks_pkg', 'compaction', '_persist', '_splitters.py',
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
    from tofu_search.search.authority import authority_label

    query = 'AWS CloudFront pricing'
    blocks = [
        _result_with_authority(
            1, 'AWS CloudFront Pricing', 'https://aws.amazon.com/cloudfront/pricing/',
            authority_label('https://aws.amazon.com/cloudfront/pricing/', query)
            if with_authority else ''),
        _result_with_authority(
            2, 'CDN comparison', 'https://latencycost.com/cdn-comparison',
            authority_label('https://latencycost.com/cdn-comparison', query)
            if with_authority else ''),
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
        assert 'Authority: Unverified — query/host token match' in index
        assert 'Unverified — aggregator-like domain name' in index
        assert 'OFFICIAL SOURCE' not in index and 'PRIMARY SOURCE' not in index
        # The query match is attached to the AWS result, not the aggregator hint.
        aws_line = next(ln for ln in index.splitlines()
                        if 'aws.amazon.com/cloudfront/pricing' in ln)
        # index blocks are multi-line; assert co-location by section slicing
        aws_idx = index.index('aws.amazon.com/cloudfront/pricing')
        agg_idx = index.index('latencycost.com')
        assert 'Unverified — query/host token match' in index[aws_idx:agg_idx]
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
