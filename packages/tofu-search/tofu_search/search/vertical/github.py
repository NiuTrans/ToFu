"""GitHub vertical — repository metadata via the GitHub REST API."""

import base64
import re

import requests

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, _HEADERS, logger

TYPE = 'github'
DOMAIN = 'code'

_EXPLICIT_RE = re.compile(
    r'^(?:github[:\s]+|gh[:\s]+)([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)', re.IGNORECASE)
_BARE_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9_-]+)/([a-zA-Z][a-zA-Z0-9_.-]+)$')
_FILE_EXTS = ('.py', '.js', '.ts', '.go', '.rs', '.rb', '.java', '.c', '.h',
              '.txt', '.md', '.yml', '.yaml', '.toml', '.cfg', '.sh', '.css', '.html')


def detect(q):
    """Detect a GitHub repo: explicit 'github:user/repo' or bare 'user/repo'."""
    m = _EXPLICIT_RE.match(q)
    if m:
        return (TYPE, m.group(1), {})
    # Bare owner/repo (conservative: must look like a repo, not a file path).
    m = _BARE_RE.match(q)
    if m and not any(m.group(2).endswith(ext) for ext in _FILE_EXTS):
        return (TYPE, q, {})
    return None


def search(identifier, params):
    """Look up a GitHub repository."""
    try:
        d = base._fetch_json(
            f'https://api.github.com/repos/{identifier}',
            headers={**_HEADERS, 'Accept': 'application/vnd.github.v3+json'}, label='GitHub',
        )
        if d is _FETCH_FAILED:
            return None

        full_name = d.get('full_name', identifier)
        description = d.get('description', '')
        stars = d.get('stargazers_count', 0)
        forks = d.get('forks_count', 0)
        language = d.get('language', '')
        lic = (d.get('license') or {})
        license_name = lic.get('spdx_id', '') or lic.get('name', '') if isinstance(lic, dict) else ''
        topics = d.get('topics', [])
        created = d.get('created_at', '')[:10]
        updated = d.get('updated_at', '')[:10]
        html_url = d.get('html_url', f'https://github.com/{identifier}')
        open_issues = d.get('open_issues_count', 0)

        parts = [f'## {full_name}']
        if description:
            parts.append(f'**Description**: {description}')
        parts.append(f'**Stars**: {stars:,}  |  **Forks**: {forks:,}  |  **Issues**: {open_issues:,}')
        if language:
            parts.append(f'**Language**: {language}')
        if license_name:
            parts.append(f'**License**: {license_name}')
        if topics:
            parts.append(f'**Topics**: {", ".join(topics[:10])}')
        parts.append(f'**Created**: {created}  |  **Updated**: {updated}')
        parts.append(f'**URL**: {html_url}')

        # Fetch README (separate request, best-effort)
        try:
            r2 = base.http_get(
                f'https://api.github.com/repos/{identifier}/readme',
                headers={**_HEADERS, 'Accept': 'application/vnd.github.v3+json'},
                timeout=8,
            )
            if r2.ok:
                content = base64.b64decode(r2.json().get('content', '')).decode('utf-8', errors='replace')
                if content:
                    parts.append(f'\n--- README ---\n{content[:2500]}')
        except (requests.RequestException, ValueError) as e:
            # ValueError covers JSON decode + base64 decode failures.
            logger.debug('[Vertical] GitHub README fetch skipped for %s: %s',
                         identifier, e)

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'GitHub'}
    except Exception as e:
        logger.warning('[Vertical] GitHub lookup failed for %s: %s', identifier, e)
        return None
