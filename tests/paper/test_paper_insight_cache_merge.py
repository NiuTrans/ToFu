"""Cached report siblings project through the production reopen path.

The owner-scoped repository has already supplied one coherent report bundle.
Projection must retain legacy markdown, structured cards and mode isolation
without starting another repository read or generation.
"""

import pytest

from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport
from routes.paper_pkg._common import _project_prefetched_report_siblings

pytestmark = pytest.mark.unit

_REPORT_BODY = '# dUltra\n\n## TL;DR\nA fast diffusion LM.\n'
_INSIGHT_BODY = '## 💡 Insight & Ideas\n\nThe grounded insight.\n'


@pytest.fixture(autouse=True)
def forbid_projection_reads(monkeypatch):
    def unexpected_read(*_args, **_kwargs):
        raise AssertionError('prefetched projection must not read storage')

    monkeypatch.setattr(PaperArtifactRepository, 'get_report', unexpected_read)
    monkeypatch.setattr(PaperArtifactRepository, 'reopen_report', unexpected_read)


def _project(body, siblings, *, lang='en', meta=None):
    return _project_prefetched_report_siblings(
        body, meta, 'paper-hash', lang, siblings)


def _insight(report=_INSIGHT_BODY, meta=None):
    return PaperReport('paper-hash', 'insight:en', report, meta=meta or {})


def test_reopen_merges_insight_once():
    siblings = {'insight:en': _insight()}
    out, _, insight, checkpoints = _project(_REPORT_BODY, siblings)
    assert out == _REPORT_BODY.rstrip() + '\n\n' + _INSIGHT_BODY
    assert insight is checkpoints is None
    assert _project(out, siblings)[0] == out


@pytest.mark.parametrize('siblings', [
    {}, {'insight:en': _insight('')}, {'insight:en': _insight('   ')},
])
def test_absent_or_empty_insight_is_noop(siblings):
    metadata = {'retained': True}
    out, meta, insight, checkpoints = _project(
        _REPORT_BODY, siblings, meta=metadata)
    assert out is _REPORT_BODY
    assert meta is metadata
    assert insight is checkpoints is None


def test_idempotent_when_body_already_has_section():
    body = _REPORT_BODY.rstrip() + '\n\n' + _INSIGHT_BODY
    assert _project(body, {'insight:en': _insight()})[0] is body


@pytest.mark.parametrize('lang', ['review:neurips:en', 'rebuttal:neurips:en'])
def test_review_mode_never_projects_plain_report_siblings(lang):
    siblings = {
        'insight:en': _insight(),
        'termfill:en': PaperReport('paper-hash', 'termfill:en', '## Terms'),
        'checkpoints:en': PaperReport('paper-hash', 'checkpoints:en', '',
                                    meta={'items': [{'question': 'Why?'}]}),
    }
    metadata = {'terminologyAudit': {'missing': ['SFT']}}
    out, meta, insight, checkpoints = _project(
        _REPORT_BODY, siblings, lang=lang, meta=metadata)
    assert out is _REPORT_BODY
    assert meta is metadata
    assert insight is checkpoints is None


def test_structured_insight_is_separate_from_report_markdown():
    items = {'anchor-1': {'summary': 'grounded', 'anchor_idx': 1}}
    usage = {'cost': 1}
    siblings = {'insight:en': _insight(meta={
        'items': items, 'baseline': {'count': 1}, 'usage': usage,
    })}
    out, _, payload, checkpoints = _project(_REPORT_BODY, siblings)
    assert out is _REPORT_BODY
    assert payload == {
        'items': items, 'baseline': {'count': 1}, 'usage': usage,
        'markdown': _INSIGHT_BODY, 'lang': 'en',
    }
    assert checkpoints is None


@pytest.mark.parametrize('meta', [None, {}, {'items': []}, {'items': 'invalid'}])
def test_legacy_or_invalid_structured_metadata_keeps_markdown(meta):
    out, _, payload, _ = _project(
        _REPORT_BODY, {'insight:en': _insight(meta=meta)})
    assert out.endswith(_INSIGHT_BODY)
    assert payload is None


def test_header_match_does_not_confuse_other_insight_headings():
    body = _REPORT_BODY + '\n## 💡 Method — How It Works\nA method.\n'
    out, _, _, _ = _project(body, {'insight:en': _insight()})
    assert '## 💡 Method — How It Works' in out
    assert out.count('## 💡 Insight & Ideas') == 1
