"""Semantic admission for composable response surfaces; no IO or execution.

The rich-response JSON schema owns shapes. This module owns relationships that
JSON schema cannot express, mirrored by frontend/rich-response/validation.ts.
"""
from datetime import datetime


def validate_surface_semantics(payload):
    blocks = payload['blocks']
    layout = payload.get('layout')
    if layout:
        groups = layout['groups']
        selected = [key for group in groups for key in group['blocks']]
        if (not groups or len({g['id'] for g in groups}) != len(groups)
                or any(not g['blocks'] for g in groups)
                or len(set(selected)) != len(selected)
                or set(selected) != {b['id'] for b in blocks}):
            raise ValueError('Layout must place every block exactly once in unique nonempty groups')
    for block in blocks:
        key = {'metric_grid': 'items', 'workflow': 'steps'}.get(block['type'])
        if key and (not block[key] or len({v['id'] for v in block[key]}) != len(block[key])):
            raise ValueError('Surface item identities must be unique and nonempty')
        if block['type'] != 'resource_panel' or 'snapshot' not in block:
            continue
        snapshot = block['snapshot']
        try:
            date = datetime.fromisoformat(snapshot['fetchedAt'].replace('Z', '+00:00'))
            if date.tzinfo is None:
                raise ValueError('Missing timezone')
        except (ValueError, TypeError) as error:
            raise ValueError('Resource snapshot needs an explicit timestamp with timezone') from error
        if len({row['id'] for row in snapshot['rows']}) != len(snapshot['rows']):
            raise ValueError('Duplicate resource row identity')
        for row in snapshot['rows']:
            for field in ('total', 'allocated', 'available', 'pending'):
                if row.get(field) is not None and row[field] < 0:
                    raise ValueError('Resource quantities cannot be negative')
