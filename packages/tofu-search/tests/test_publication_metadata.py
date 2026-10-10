"""Publication metadata agrees with the established BeautifulSoup parser."""
from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from tofu_search.fetch import html_extract


class SoupOracle:
    def feed(self, source):
        self.soup = BeautifulSoup(source, 'html.parser')

    def close(self):
        pass

    def find(self, *args, **kwargs):
        return self.soup.find(*args, **kwargs)

    def find_all(self, *args, **kwargs):
        return self.soup.find_all(*args, **kwargs)


def assert_matches_oracle(monkeypatch, source):
    observed = html_extract.extract_html_publish_date(source)
    with monkeypatch.context() as context:
        context.setattr(html_extract, '_PublicationMetadata', SoupOracle)
        expected = html_extract.extract_html_publish_date(source)
    assert observed == expected, repr(source)
    return observed


@pytest.mark.parametrize('source,expected', [
    ('<meta property="article:published_time" content="2024-02-03T01:00:00+03:00">', '2024-02-02'),
    ('<meta name="DC.date.İssued" content="2024-02-03">', '2024-02-03'),
    ('<script type="application/ld+json">{"datePublished":"2024-01-01"}</script>', '2024-01-01'),
    ('<script type="application/ld+json">{"datePublished":"2024-01-01"}', '2024-01-01'),
    ('<script type="application/ld+json">{"@graph":[{"dateCreated":"2024-01-01"}]}</script>', '2024-01-01'),
    ('<script type="application/ld+json"/><time datetime="2024-01-01">', '2024-01-01'),
    ('<meta property="article:published_time" content="invalid"><meta property="article:published_time" '
     'content="2024-01-01"><time datetime="2023-02-03">', '2023-02-03'),
    ('<!--<meta name="date" content="2024-01-01">--><style>meta {content: "2024-01-01"}</style>', ''),
    ('<meta property="article:published_time" content="1999-01-01"><time datetime="2024-01-01">', '2024-01-01'),
    ('<meta property="article:published_time" content="2999-01-01">', ''),
])
def test_metadata_priority_encoding_and_malformed_carriers(monkeypatch, source, expected):
    assert assert_matches_oracle(monkeypatch, source) == expected


def test_bounded_prefix_and_nonstring_carriers_remain_compatible(monkeypatch):
    sources = [b'<meta name="date" content="2024-01-01">', '', None,
               'x' * 50000 + '<meta name="date" content="2024-01-01">',
               '<script type="application/ld+json">' + ' ' * 49950 + '{"datePublished":"2024-01-01"}</script>']
    for source in sources:
        assert_matches_oracle(monkeypatch, source)


def test_nested_noisy_pages_match_dom_parser(monkeypatch):
    prefixes = ['<html><head>', '<p><b>broken', '<svg>', '<template>', '<textarea>',
                '<script>ignored<meta name="date" content="2021-01-01"></script>',
                '<style>ignored<meta name="date" content="2021-01-01"></style>']
    carriers = ['<META NAME="pubdate" CONTENT="2024-03-04">',
                '<meta name="pubdate" name="date" content="2024-03-04">',
                '<meta name="date" content="2024&#45;03&#45;04">',
                '<script type="application/ld+json">[{"datePublished":"2024-03-04"}]</script>',
                '<time datetime="2024-03-04">text</time>']
    for prefix in prefixes:
        for first in carriers:
            for second in carriers:
                assert_matches_oracle(monkeypatch, prefix + first + '</bad>' + second)
