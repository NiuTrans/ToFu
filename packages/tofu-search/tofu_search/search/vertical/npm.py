"""npm vertical — npm registry package metadata."""

import re

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, logger

TYPE = 'npm'
DOMAIN = 'code'

_DETECT_RE = re.compile(
    r'^(?:npm[:\s]+(?:install\s+|i\s+|info\s+)?|npx\s+)([a-zA-Z@][a-zA-Z0-9._@/-]*)',
    re.IGNORECASE)


def detect(q):
    """Detect an explicit npm intent ('npm X', 'npm:X', 'npm install X', 'npx X')."""
    m = _DETECT_RE.match(q)
    if m:
        return (TYPE, m.group(1), {})
    return None


def _clean_repo_url(repo_url):
    """Normalise a registry repository URL and strip a trailing .git suffix.

    NOTE: a plain ``str.rstrip('.git')`` is WRONG — it strips any trailing
    chars in the set {'.','g','i','t'} (e.g. 'digit' → 'di'). We only want to
    remove a literal ``.git`` suffix.
    """
    clean = repo_url.replace('git+', '').replace('git://', 'https://')
    if clean.endswith('.git'):
        clean = clean[:-len('.git')]
    return clean


def search(identifier, params):
    """Look up an npm package on the registry."""
    try:
        data = base._fetch_json(f'https://registry.npmjs.org/{identifier}', timeout=8, label='npm')
        if data is _FETCH_FAILED:
            return None

        name = data.get('name', identifier)
        description = data.get('description', '')
        latest_ver = (data.get('dist-tags') or {}).get('latest', '')
        license_ = data.get('license', '')
        if isinstance(license_, dict):
            license_ = license_.get('type', '')
        homepage = data.get('homepage', '')
        repo = data.get('repository', {})
        repo_url = repo.get('url', '') if isinstance(repo, dict) else str(repo)
        readme = data.get('readme', '')

        maintainers = data.get('maintainers', [])
        maint_names = [m.get('name', '') for m in maintainers[:5] if isinstance(m, dict)]

        parts = [f'## {name} {latest_ver}']
        if description:
            parts.append(f'**Description**: {description}')
        if license_:
            parts.append(f'**License**: {license_}')
        if maint_names:
            parts.append(f'**Maintainers**: {", ".join(maint_names)}')
        parts.append(f'**npm**: https://www.npmjs.com/package/{name}')
        if homepage:
            parts.append(f'**Homepage**: {homepage}')
        if repo_url:
            parts.append(f'**Repository**: {_clean_repo_url(repo_url)}')
        if readme and readme != 'ERROR: No README data found!':
            parts.append(f'\n{readme[:2000]}')

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'npm'}
    except Exception as e:
        logger.warning('[Vertical] npm lookup failed for %s: %s', identifier, e)
        return None
