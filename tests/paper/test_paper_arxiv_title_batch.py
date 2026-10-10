"""Cost-bounded arXiv title batching for survey evidence grounding."""

from __future__ import annotations

from urllib.parse import unquote

import pytest


pytestmark = pytest.mark.unit


_ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2608.00001v2</id>
    <title> First\n title </title>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2608.00002</id>
    <title>Second title</title>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2608.99999</id>
    <title>Unrequested</title>
  </entry>
</feed>'''


def test_title_batch_deduplicates_and_uses_exactly_one_request(monkeypatch):
    import lib.paper.arxiv as arxiv

    calls = []

    class Response:
        content = _ATOM

        @staticmethod
        def raise_for_status():
            return None

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(arxiv, 'http_get', get)
    got = arxiv.fetch_arxiv_titles_batch([
        '2608.00001v3', '2608.00001', '2608.00002', '2608.00003'])

    assert got == {
        '2608.00001': 'First title',
        '2608.00002': 'Second title',
    }
    assert len(calls) == 1
    assert 'id_list=2608.00001%2C2608.00002%2C2608.00003' in calls[0][0]
    assert 'max_results=3' in calls[0][0]
    assert calls[0][1]['timeout'] == 15
    assert unquote(calls[0][0]).count('2608.00001') == 1


def test_title_batch_rejects_oversize_before_http(monkeypatch):
    import lib.paper.arxiv as arxiv

    monkeypatch.setattr(
        arxiv, 'http_get',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('oversize title batch must fail before HTTP')))
    with pytest.raises(ValueError, match='at most 20'):
        arxiv.fetch_arxiv_titles_batch(
            [f'2608.{index:05d}' for index in range(21)])
