"""Backend-authored FieldSpec builders and OpenAPI schema projection."""

from __future__ import annotations


VALID_PARAM_KINDS = frozenset({
    'text', 'textarea', 'select', 'list', 'int', 'bool',
})


def field_spec(key: str, kind: str, label: str, **metadata) -> dict:
    """Build one serializable FieldSpec for role/control schema tables."""
    spec = {'key': key, 'kind': kind, 'label': label}
    spec.update({
        name: value for name, value in metadata.items()
        if value is not None
    })
    return spec


def field_spec_schema() -> dict:
    """Describe one backend-authored role/control editor field."""
    return {
        'type': 'object',
        'required': ['key', 'kind', 'label'],
        'additionalProperties': True,
        'properties': {
            'key': {'type': 'string'},
            'kind': {
                'type': 'string', 'enum': sorted(VALID_PARAM_KINDS),
            },
            'label': {'type': 'string'},
            'heading': {'type': 'string'},
            'placeholder': {'type': 'string'},
            'options': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'required': ['value', 'label'],
                    'additionalProperties': True,
                    'properties': {
                        'value': {'type': 'string'},
                        'label': {'type': 'string'},
                        'disabled': {'type': 'boolean'},
                    },
                },
            },
            'min': {'type': 'integer'},
            'max': {'type': 'integer'},
            'runtimeMax': {'type': 'integer', 'minimum': 1},
            'maxLength': {'type': 'integer', 'minimum': 1},
            'maxItems': {'type': 'integer', 'minimum': 1},
            'maxItemLength': {'type': 'integer', 'minimum': 1},
            'allowUnknown': {'type': 'boolean'},
            'severity': {'type': 'string', 'enum': ['error', 'warning']},
            'errorName': {'type': 'string'},
            'visibleWhen': {
                'type': 'object',
                'required': ['key', 'equals'],
                'additionalProperties': True,
                'properties': {
                    'key': {'type': 'string'},
                    'equals': {},
                },
            },
        },
    }


def field_spec_list_schema() -> dict:
    return {'type': 'array', 'items': field_spec_schema()}


def field_spec_registry_schema(registry: dict) -> dict:
    """Describe one named registry of FieldSpec lists."""
    names = list(registry)
    return {
        'type': 'object',
        'required': names,
        'additionalProperties': False,
        'properties': {
            name: field_spec_list_schema()
            for name in names
        },
    }


__all__ = [
    'VALID_PARAM_KINDS', 'field_spec', 'field_spec_schema',
    'field_spec_list_schema', 'field_spec_registry_schema',
]
