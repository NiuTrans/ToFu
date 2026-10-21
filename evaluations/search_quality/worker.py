"""One disposable free-search/fetch operation; JSON input/output on stdio.

The parent owns the process group and wall-clock budget. Proxy credentials
arrive on stdin, never argv or artifacts. Provider errors expose type only.
"""
from __future__ import annotations

import contextlib
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import random
import sys
import time
import tomllib

from .contracts import DDGS_VERSION, PAGE_CHARS, RESULTS, SNIPPET_CHARS, python_tree_digest, url_key


def source_trace(groups, *, url_field='url'):
    """Bound raw source observations for opt-in diagnostics, never fetch authority."""
    candidates, used, truncated = [], 512, False
    for engine, rows in groups.items():
        for row in rows:
            url = row.get(url_field, '')
            if not isinstance(url, str) or len(url) > 4000 or not url_key(url):
                continue
            item = {'engine': str(engine)[:80], 'url': url}
            size = len(json.dumps(item, ensure_ascii=False).encode()) + 1
            if len(candidates) >= 256 or used + size > 64 * 1024:
                truncated = True
                continue
            candidates.append(item)
            used += size
    return {'candidates': candidates, 'truncated': truncated,
            'scope': 'Raw engine observations; not ranked results or fetch authority'}


def search_observation(rows, *, url_field='url', snippet_field='snippet'):
    """Keep discovery candidates and safe diagnostics within the shared budget.

    Search snippets and uncertain follow-ups are not fetched-source evidence.
    URL strings must remain exact: truncating one would invent fetch authority.
    """
    followups = getattr(rows, '_unverified_candidates', None) or []
    results, seen, omitted_urls = [], set(), 0
    for candidates, status in ((rows, 'search_snippet'), (followups, 'unverified_candidate')):
        for row in candidates:
            if len(results) == RESULTS:
                break
            url = row.get(url_field, '')
            key = url_key(url) if isinstance(url, str) and len(url) <= 4000 else ''
            if not key:
                omitted_urls += 1
                continue
            if key in seen:
                continue
            seen.add(key)
            results.append({'title': str(row.get('title') or '')[:500], 'url': url,
                            'snippet': str(row.get(snippet_field) or '')[:SNIPPET_CHARS],
                            'evidence_status': status})
    diagnostic = {'omitted_url_count': omitted_urls}
    native = getattr(rows, '_search_diag', None) or {}
    # Do not expose arbitrary provider messages, which may contain route details.
    if native:
        reason = native.get('reason')
        if reason in {'no_usable_evidence', 'network_error', 'partial_network_error',
                      'no_results', 'deadline'}:
            diagnostic['reason'] = reason
        for name in ('unverified_count', 'weak_match_count', 'usable_count', 'constraint_rejected'):
            value = native.get(name)
            if type(value) is int and value >= 0:
                diagnostic[name] = value
        diagnostic['engine_error_count'] = len(native.get('engine_errors') or {})
        diagnostic['deadline_hit'] = bool(native.get('deadline_hit'))
    return {'results': results, 'search_diagnostic': diagnostic}


def identity(arm):
    if arm == 'ddgs':
        import ddgs
        import primp  # noqa: F401 -- verify the installed native extension can load
        module = ddgs
    else:
        import tofu_search
        module = tofu_search
    names = (('ddgs', 'primp', 'lxml') if arm == 'ddgs' else
             ('tofu-search', 'requests', 'httpx', 'trafilatura', 'lxml',
              'playwright', 'pymupdf', 'pymupdf4llm'))
    versions = {'python': sys.version, 'entry_sha256':
                hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
    if arm == 'tofu':
        versions['tofu_source_sha256'] = python_tree_digest(Path(module.__file__).parent)
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = 'source-checkout' if name == 'tofu-search' else 'not_installed'
    if arm == 'tofu':
        # Workers deliberately load the current checkout. Its ignored egg-info
        # can lag the source or shadow metadata from the selected interpreter.
        declaration = Path(module.__file__).resolve().parent.parent / 'pyproject.toml'
        if declaration.is_file():
            project = tomllib.loads(declaration.read_text(encoding='utf-8')).get('project', {})
            if project.get('name') == 'tofu-search':
                version = project.get('version')
                if not isinstance(version, str) or not version.strip():
                    raise ValueError('Loaded tofu-search source has no declared version')
                versions['tofu-search'] = version
    if arm == 'ddgs':
        if versions['ddgs'] != DDGS_VERSION:
            raise ValueError('Unexpected DDGS version')
        dist = metadata.distribution('ddgs')
        files = sorted(str(p) for p in dist.files if str(p).endswith('.py'))
        versions['ddgsSourceSha256'] = hashlib.sha256(''.join(
            p + ':' + hashlib.sha256(Path(dist.locate_file(p)).read_bytes()).hexdigest()
            for p in files).encode()).hexdigest()
    return versions


def operation(request):
    arm = request['arm']
    if request['operation'] == 'identity':
        return {'identity': identity(arm)}
    for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                'ALL_PROXY', 'DDGS_PROXY', 'TOFU_SEARCH_SEARXNG_URL',
                'TOFU_SEARCH_SEARXNG_ENGINES'):
        os.environ.pop(key, None)
    proxy = request['proxy']
    random.seed(request['seed'])
    if request['operation'] == 'search' and arm == 'ddgs':
        identity(arm)
        from ddgs import DDGS
        with DDGS(proxy=proxy or None, timeout=10, verify=True) as engine:
            rows = engine.text(request['query'], region=request['region'],
                               safesearch='moderate', max_results=RESULTS, backend='auto')
        observation = search_observation(rows, url_field='href', snippet_field='body')
        if request.get('include_source_trace') is True:
            observation['source_trace'] = source_trace({'DDGS-auto': rows}, url_field='href')
        return observation
    from tofu_search import SearchConfig, fetch_page_content, get_playwright_pool, perform_web_search
    from tofu_search.config import bind_call_config
    cfg = SearchConfig(proxy_url=proxy, proxy_dual_attempt=False,
                       search_deadline_secs=40, fetch_url_deadline_secs=30,
                       searxng_url='', searxng_engines='')
    try:
        if request['operation'] == 'search' and arm == 'tofu':
            rows = perform_web_search(request['query'], max_results=RESULTS,
                                      fetch_pages=False, filter_pages=False, config=cfg)
            observation = search_observation(rows)
            if request.get('include_source_trace') is True:
                observation['source_trace'] = source_trace(
                    getattr(rows, '_engine_breakdown', None) or {'returned': rows})
            return observation
        if request['operation'] == 'fetch':
            diag = {}
            with bind_call_config(cfg):
                body = fetch_page_content(request['url'], max_chars=PAGE_CHARS, timeout=10,
                                          diag=diag, query=request.get('query'))
            if not body:
                return {'error': 'fetch_unavailable', 'worker_fetch_reason': diag.get('reason')}
            return {'url': request['url'], 'text': str(body)[:PAGE_CHARS],
                    'truncated': len(str(body)) > PAGE_CHARS or bool(diag.get('truncated'))}
        raise ValueError('Unsupported operation')
    finally:
        get_playwright_pool().shutdown()


def main():
    request = json.loads(sys.stdin.buffer.read(16385))
    started = time.monotonic()
    # Third-party logs may contain request/proxy details; keep them out of the
    # evidence channel. Typed failures remain visible in the operation receipt.
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            result = operation(request)
        except Exception as exc:
            result = {'error': type(exc).__name__}
    result['operation_seconds'] = time.monotonic() - started
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
