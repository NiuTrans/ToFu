"""PyPI vertical — Python package metadata."""

import re

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, logger

TYPE = 'pypi'
DOMAIN = 'code'

_DETECT_RE = re.compile(
    r'^(?:pypi[:\s]+|pip\s+install\s+)([a-zA-Z][a-zA-Z0-9._-]*)', re.IGNORECASE)


def detect(q):
    """Detect an explicit PyPI intent ('pypi X' / 'pip install X')."""
    m = _DETECT_RE.match(q)
    if m:
        return (TYPE, m.group(1).lower(), {})
    return None


def search(identifier, params):
    """Look up a Python package on PyPI."""
    try:
        data = base._fetch_json(f'https://pypi.org/pypi/{identifier}/json', label='PyPI')
        if data is _FETCH_FAILED:
            return None
        info = data.get('info', {})

        name = info.get('name', identifier)
        version = info.get('version', '')
        summary = info.get('summary', '')
        author = info.get('author', '') or info.get('author_email', '')
        license_ = info.get('license', '')
        requires_python = info.get('requires_python', '')
        project_urls = info.get('project_urls') or {}
        description = info.get('description', '')

        parts = [f'## {name} {version}']
        if summary:
            parts.append(f'**Summary**: {summary}')
        if author:
            parts.append(f'**Author**: {author}')
        if license_:
            parts.append(f'**License**: {license_[:100]}')
        if requires_python:
            parts.append(f'**Python**: {requires_python}')
        parts.append(f'**PyPI**: https://pypi.org/project/{name}/')
        for label, url in list(project_urls.items())[:4]:
            parts.append(f'**{label}**: {url}')
        if description:
            parts.append(f'\n{description[:2000]}')

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'PyPI'}
    except Exception as e:
        logger.warning('[Vertical] PyPI lookup failed for %s: %s', identifier, e)
        return None
