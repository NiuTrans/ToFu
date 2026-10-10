"""Explicit synthetic registration evidence for tests that install new models."""
from copy import deepcopy


def complete_registration(document):
    document = deepcopy(document)
    for model in document['models']:
        model.setdefault('registration_metadata', {'release_date': '2026-01-01',
            'checked_at': '2026-09-18', 'source_urls': ['https://example.invalid/test-model']})
        model.setdefault('list_pricing', {'input': 1, 'output': 2, 'currency': 'USD', 'unit': 'per_million_tokens'})
        model.setdefault('inference_profile', {'reasoning': ({'support': 'supported',
            'wire_format': 'thinking_type', 'can_disable': True, 'supported_efforts': []}
            if 'thinking' in model.get('capabilities', []) else {'support': 'unsupported'})})
    return document
