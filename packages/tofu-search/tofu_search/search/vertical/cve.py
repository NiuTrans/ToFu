"""CVE vertical — NVD (NIST) lookup."""

import re

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, logger

TYPE = 'cve'
DOMAIN = 'security'


def detect(q):
    """Detect a CVE id anywhere in the query."""
    m = re.search(r'(CVE-\d{4}-\d{4,7})', q, re.IGNORECASE)
    if m:
        return (TYPE, m.group(1).upper(), {})
    return None


def search(identifier, params):
    """Query NVD (NIST) for CVE details."""
    try:
        data = base._fetch_json(
            'https://services.nvd.nist.gov/rest/json/cves/2.0',
            params={'cveId': identifier}, label='CVE',
        )
        if data is _FETCH_FAILED:
            return None
        vulns = data.get('vulnerabilities', [])
        if not vulns:
            return None

        cve = vulns[0].get('cve', {})
        desc_list = cve.get('descriptions', [])
        desc = next((d['value'] for d in desc_list if d.get('lang') == 'en'), '')

        cvss_score, severity = '', ''
        for vk in ('cvssMetricV31', 'cvssMetricV30', 'cvssMetricV2'):
            metrics_list = cve.get('metrics', {}).get(vk, [])
            if metrics_list:
                cd = metrics_list[0].get('cvssData', {})
                cvss_score = str(cd.get('baseScore', ''))
                severity = cd.get('baseSeverity', '')
                break

        refs = [r['url'] for r in cve.get('references', [])[:5]]
        published = cve.get('published', '')[:10]
        modified = cve.get('lastModified', '')[:10]

        parts = [f'## {identifier}']
        if severity and cvss_score:
            parts.append(f'**CVSS Score**: {cvss_score} ({severity})')
        if published:
            parts.append(f'**Published**: {published}  |  **Modified**: {modified}')
        parts.append(f'\n**Description**: {desc}')
        if refs:
            parts.append('\n**References**:\n' + '\n'.join(f'- {u}' for u in refs))

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'NVD (NIST)'}
    except Exception as e:
        logger.warning('[Vertical] CVE lookup failed for %s: %s', identifier, e)
        return None
