"""Executable specification for the search.skip_domains URL-blocklist grammar.

The packaged copy (tofu_search/generated_url_blocklist.py) is generated from
lib/search/url_blocklist.py; these tests exercise the packaged module so the search
pipeline's matching is the same rule the host enforces.
"""
import pytest

from tofu_search.generated_url_blocklist import blocked_url, url_is_blocked

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('url', [
    'https://example.com/page',
    'https://example.com:8443/page',
    'https://www.example.com/page',
    'https://a.b.example.com/',
    'http://EXAMPLE.COM/page?x=1#frag',
])
def test_host_rule_covers_host_and_subdomains(url):
    assert blocked_url(url, {'example.com'}) == 'example.com'


@pytest.mark.parametrize('url', [
    'https://dropbox.com/file',            # historical substring match: "x.com" in netloc
    'https://example.com.evil.test/page',
    'https://notexample.com/',
    'https://example.org/',
    'file:///etc/passwd',
    '/local/relative/path',
    '',
])
def test_host_rule_does_not_match_other_hosts(url):
    assert blocked_url(url, {'example.com'}) is None


@pytest.mark.parametrize('url', [
    'https://raw.githubusercontent.com/harbor-framework/terminal-bench/main/tests/x.py',
    'https://raw.githubusercontent.com/harbor-framework',
])
def test_prefix_on_a_raw_content_host(url):
    assert blocked_url(url, {'raw.githubusercontent.com/harbor-framework'})


def test_api_host_prefix_covers_the_repository_contents_route():
    # api.github.com inserts the /repos namespace before owner/repo, so the
    # source_guard projection emits both spellings (tofu_skip_domains).
    assert blocked_url('https://api.github.com/repos/harbor-framework/terminal-bench/contents/x',
                       {'api.github.com/repos/harbor-framework'})
    assert blocked_url('https://api.github.com/harbor-framework/terminal-bench',
                       {'api.github.com/harbor-framework'})
    assert blocked_url('https://api.github.com/repos/python/cpython/contents/x',
                       {'api.github.com/repos/harbor-framework'}) is None


def test_host_rule_is_boundary_aware_for_the_default_social_list():
    # Before the boundary-aware rule the default entry 'x.com' refused Dropbox.
    assert url_is_blocked('https://www.dropbox.com/s/abc/file.pdf', {'x.com'}) is False
    assert url_is_blocked('https://x.com/user/status/1', {'x.com'}) is True
    assert url_is_blocked('https://mobile.twitter.com/a', {'twitter.com'}) is True


@pytest.mark.parametrize('url', [
    'https://github.com/harbor-framework/terminal-bench-2-1/blob/main/README.md',
    'https://github.com/harbor-framework',
    'https://github.com/harbor-framework-mirror/other',   # prefix semantics
    'https://raw.githubusercontent.com/harbor-framework/terminal-bench/main/tests/x.py',
])
def test_url_prefix_blocks_only_that_path(url):
    host = 'raw.githubusercontent.com' if 'raw.githubusercontent' in url else 'github.com'
    assert blocked_url(url, {host + '/harbor-framework'})


@pytest.mark.parametrize('url', [
    'https://github.com/python/cpython/blob/main/README.rst',
    'https://github.com/otherorg/harbor-framework-notes',
    'https://github.com/',
])
def test_other_repositories_on_the_same_host_stay_allowed(url):
    assert blocked_url(url, {'github.com/harbor-framework'}) is None


def test_wildcard_segment_covers_every_owner():
    entry = 'github.com/*/terminal-bench'
    assert blocked_url('https://github.com/lynl7130/terminal-bench/blob/main/x', {entry})
    assert blocked_url('https://github.com/a/terminal-bench', {entry})
    assert blocked_url('https://github.com/a/terminal-bench/tasks/x', {entry})
    assert blocked_url('https://github.com/a/other-repo', {entry}) is None
    # A wildcard matches exactly one segment, never zero or two.
    assert blocked_url('https://github.com/terminal-bench', {entry}) is None


def test_globstar_segment_matches_any_depth():
    # A mirror nests the benchmark directory deep inside another repository.
    entry = 'github.com/**/terminal-bench*'
    assert blocked_url('https://github.com/eigent-ai/agenticrl-bench/tree/main/tasks/x/tests/'
                       '__image__/tb21/terminal-bench-2-1/torch-pipeline-parallelism/tests', {entry})
    assert blocked_url('https://github.com/terminal-bench', {entry})  # zero segments before it
    assert blocked_url('https://github.com/a/terminal-bench/x', {entry})
    assert blocked_url('https://github.com/a/b/other', {entry}) is None
    assert blocked_url('https://github.com/a/my-terminal-bench', {entry}) is None


def test_star_inside_a_segment_matches_within_that_segment_only():
    entry = 'huggingface.co/datasets/*/*terminal-bench*'
    assert blocked_url('https://huggingface.co/datasets/harborframework/terminal-bench-2.0/raw/main/'
                       'torch-pipeline-parallelism/tests/test_outputs.py', {entry})
    assert blocked_url('https://huggingface.co/datasets/x/my-terminal-bench-copy', {entry})
    assert blocked_url('https://huggingface.co/datasets/x/other', {entry}) is None
    # ``*`` never crosses a ``/``.
    assert blocked_url('https://huggingface.co/datasets/terminal/x/bench',
                       {'huggingface.co/datasets/terminal*bench'}) is None
    # Other characters stay literal, including regex metacharacters.
    assert blocked_url('https://example.com/a.b', {'example.com/a.b*'})
    assert blocked_url('https://example.com/axb', {'example.com/a.b*'}) is None


def test_star_host_with_a_path_pattern_matches_every_host():
    entry = '*/**/*terminal-bench*'
    assert blocked_url('https://mirror.example.org/archive/terminal-bench-2/x', {entry}) == entry
    assert blocked_url('https://raw.githubusercontent.com/a/b/main/terminal-bench/x', {entry})
    assert blocked_url('https://r.jina.ai/https://github.com/x/terminal-bench', {entry})
    assert blocked_url('https://example.org/docs/other', {entry}) is None


def test_a_bare_star_host_is_ignored_rather_than_refusing_every_url():
    for entry in ('*', '*/', 'https://*'):
        assert blocked_url('https://example.com/x', {entry}) is None


def test_scheme_and_query_do_not_change_matching():
    entries = {'https://example.com/private'}
    assert blocked_url('https://example.com/private/file?token=abc', entries)
    assert blocked_url('http://example.com/private', entries)
    assert blocked_url('https://example.com/public', entries) is None


def test_port_rule_pins_the_port():
    assert blocked_url('http://localhost:8080/admin', {'localhost:8080'})
    assert blocked_url('http://localhost:9090/admin', {'localhost:8080'}) is None
    assert blocked_url('http://localhost/admin', {'localhost:8080'}) is None


def test_blank_and_malformed_entries_are_ignored():
    entries = {'', '   ', 'https://', None}
    assert blocked_url('https://example.com/x', entries) is None


def test_the_first_matching_entry_is_reported():
    entries = ['example.com/private', 'example.com']
    assert blocked_url('https://example.com/private/x', entries) == 'example.com/private'


def test_case_insensitive_entry_and_url():
    assert blocked_url('https://GitHub.com/Harbor-Framework/x', {'github.com/harbor-framework'})
    assert blocked_url('https://github.com/HARBOR-FRAMEWORK/x', {'github.com/harbor-framework'})


def test_urls_with_credentials_and_fragments():
    assert blocked_url('https://user:pw@example.com/private', {'example.com/private'})
    assert blocked_url('https://example.com/private#section', {'example.com/private'})


def test_non_http_schemes_never_match():
    assert blocked_url('mailto:user@example.com', {'example.com'}) is None
    assert blocked_url('ftp://example.com/x', {'example.com'}) is None
