"""Machine-readable fixed-source extraction contract and grounded projection.

The helper model proposes findings and unresolved requests; this owner validates
quotes against received evidence and computes offsets. It never proves semantic
entailment, website completeness or absence. No transport or persistent state.
"""

import json
import re

EXTRACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['findings', 'unresolved'],
    'properties': {
        'findings': {'type': 'array', 'maxItems': 12, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['fact', 'quote'], 'properties': {
                'fact': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                'quote': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}}},
        'unresolved': {'type': 'array', 'maxItems': 12,
                       'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}},
    },
}


def project_extraction(reply, *, source, excerpt, exhaustive):
    """Validate the bounded response and render only source-addressable findings."""
    if len(reply) > 48_000:
        raise ValueError('extraction exceeds response contract')
    payload = json.loads(reply)
    if not isinstance(payload, dict) or set(payload) != set(EXTRACTION_SCHEMA['required']):
        raise ValueError('extraction requires findings and unresolved')
    for key, schema in EXTRACTION_SCHEMA['properties'].items():
        items = payload[key]
        if not isinstance(items, list) or len(items) > schema['maxItems']:
            raise ValueError('extraction item count violates contract')
        for item in items:
            fields = schema['items']
            if fields['type'] == 'object':
                if not isinstance(item, dict) or set(item) != set(fields['required']):
                    raise ValueError('each finding requires fact and quote')
                pairs = [(item[name], spec) for name, spec in fields['properties'].items()]
            else:
                pairs = [(item, fields)]
            for value, spec in pairs:
                if (not isinstance(value, str) or not value.strip()
                        or not spec['minLength'] <= len(value) <= spec['maxLength']):
                    raise ValueError('extraction text violates contract')
    if not payload['findings'] and not payload['unresolved']:
        raise ValueError('empty extraction does not answer or identify a gap')
    lines = [
        'Extraction scope: ' + ('all received text' if exhaustive else 'selected windows of received text')
        + '; fetched text is not proof of complete page or website coverage.',
        'Findings with verbatim support (verify meaning against the source):',
    ]
    for finding in payload['findings']:
        quote = finding['quote']
        position = source.find(quote)
        if position < 0 or quote not in excerpt:
            raise ValueError('finding quote is not present in the inspected evidence')
        lines.append(f"- {finding['fact']}\n  Source chars [{position},{position + len(quote)}): {quote}")
    if not payload['findings']:
        lines.append('- No supported finding extracted; this is not evidence of absence.')
    if payload['unresolved']:
        lines.append('Unresolved requests (not verified negative facts):')
        lines.extend('- ' + gap for gap in payload['unresolved'])
        # Follow actual fetched links, never guess a URL from a model name.
        links = list(dict.fromkeys(re.findall(r'\[[^\]\n]{1,160}\]\(https?://[^\s)]+\)', excerpt)))[:8]
        if links:
            lines.append('Source links for follow-up:')
            lines.extend('- ' + link for link in links)
        lines.append('Search/read the retained source to resolve gaps before making negative or exhaustive claims.')
    return '\n'.join(lines)
