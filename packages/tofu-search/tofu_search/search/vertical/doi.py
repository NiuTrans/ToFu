"""DOI vertical — CrossRef lookup."""

import re

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, _HEADERS, logger

TYPE = 'doi'
DOMAIN = 'academic'

# A DOI: 10.<registrant>/<suffix>. Matches anywhere in the query (not just at
# the start), with an optional "doi:" prefix. Trailing sentence punctuation is
# trimmed by the caller via _strip_doi.
_DOI_RE = re.compile(r'(?:doi[:\s]+)?(10\.\d{4,}/\S+)', re.IGNORECASE)


def strip_doi(doi):
    """Trim trailing sentence punctuation a DOI shouldn't end with."""
    return doi.rstrip('.,;:)>]\'"')


def detect(q):
    """Detect a DOI anywhere in the query."""
    m = _DOI_RE.search(q)
    if m:
        return (TYPE, strip_doi(m.group(1)), {})
    return None


def search(identifier, params):
    """Look up a DOI via CrossRef."""
    try:
        data = base._fetch_json(
            f'https://api.crossref.org/works/{identifier}',
            headers={**_HEADERS, 'Accept': 'application/json'}, label='DOI',
        )
        if data is _FETCH_FAILED:
            return None
        msg = data.get('message', {})

        title = (msg.get('title', [''])[0] or identifier)
        authors = []
        for a in msg.get('author', [])[:10]:
            name = f"{a.get('given', '')} {a.get('family', '')}".strip()
            if name:
                authors.append(name)

        container = (msg.get('container-title', ['']) or [''])[0]
        pub_parts = (msg.get('published-print') or msg.get('published-online') or {}).get('date-parts', [[]])
        published = '-'.join(str(x) for x in pub_parts[0]) if pub_parts and pub_parts[0] else ''
        abstract = re.sub(r'<[^>]+>', '', msg.get('abstract', '')).strip()
        url = msg.get('URL', f'https://doi.org/{identifier}')

        parts = [f'## {title}', f'**DOI**: {identifier}']
        if authors:
            parts.append(f'**Authors**: {", ".join(authors)}')
        if container:
            parts.append(f'**Journal**: {container}')
        if published:
            parts.append(f'**Published**: {published}')
        parts.append(f'**URL**: {url}')
        if abstract:
            parts.append(f'\n**Abstract**: {abstract[:1500]}')

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'CrossRef'}
    except Exception as e:
        logger.warning('[Vertical] DOI lookup failed for %s: %s', identifier, e)
        return None
