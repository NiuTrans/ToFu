"""Structured, per-user context store and bounded undo history.

The legacy personal profile was a Markdown file whose headings accidentally
encoded runtime semantics.  This module makes those semantics explicit while
keeping the Markdown API as a compatibility projection.
"""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone

from lib.ids import short_id
from lib.json_store import read_json, write_json_atomic, write_text_atomic
from lib.log import audit_log, get_logger

from lib.memory.user_profile._paths import (
    context_changes_path,
    context_path,
    profile_path,
)

logger = get_logger(__name__)

CONTEXT_VERSION = 1
CONTEXT_TYPES = ('identity', 'work_rule', 'response_preference')
CONTEXT_SOURCES = ('manual', 'assistant', 'legacy_migration')
CONTEXT_CHAR_CAP = 2500
CONTEXT_CHANGE_LIMIT = 50


class ContextValidationError(ValueError):
    """A context item or document failed validation."""


class ContextConflictError(RuntimeError):
    """An undo could not be applied without overwriting a newer edit."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _clean_text(value) -> str:
    return str(value or '').strip()


def _normalize_item(raw: dict, *, source_default: str = 'manual',
                    now: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        raise ContextValidationError('Each context item must be an object')
    item_type = _clean_text(raw.get('type'))
    if item_type not in CONTEXT_TYPES:
        raise ContextValidationError(f'Unsupported context type: {item_type or "(empty)"}')
    source = _clean_text(raw.get('source')) or source_default
    if source not in CONTEXT_SOURCES:
        source = source_default if source_default in CONTEXT_SOURCES else 'manual'
    stamp = now or _now()
    item = {
        'id': _clean_text(raw.get('id')) or short_id('ctx_', 16),
        'type': item_type,
        'source': source,
        'created_at': _clean_text(raw.get('created_at')) or stamp,
        'updated_at': _clean_text(raw.get('updated_at')) or stamp,
    }
    if item_type == 'work_rule':
        condition = _clean_text(raw.get('condition'))
        action = _clean_text(raw.get('action'))
        if not condition or not action:
            raise ContextValidationError(
                'A work rule requires both condition and action')
        item.update({'condition': condition, 'action': action})
    else:
        text = _clean_text(raw.get('text'))
        if not text:
            return None
        item['text'] = text
    return item


def context_markdown(items: list[dict]) -> str:
    """Stable, human-readable content used for the cap and prompt body."""
    groups = {
        'work_rule': [],
        'response_preference': [],
        'identity': [],
    }
    for item in items or []:
        item_type = item.get('type')
        if item_type == 'work_rule':
            groups[item_type].append(
                f'- WHEN: {item.get("condition", "").strip()}\n'
                f'  DO: {item.get("action", "").strip()}')
        elif item_type in groups and item.get('text'):
            groups[item_type].append(f'- {item["text"].strip()}')
    sections = []
    labels = (
        ('work_rule', 'Work rules'),
        ('response_preference', 'Response preferences'),
        ('identity', 'About the user'),
    )
    for key, label in labels:
        if groups[key]:
            sections.append(f'## {label}\n' + '\n'.join(groups[key]))
    return '\n\n'.join(sections).strip()


def context_char_count(items: list[dict]) -> int:
    return len(context_markdown(items))


def _legacy_markdown(items: list[dict]) -> str:
    """Project structured items onto the old ``## Header`` wire shape."""
    sections: list[str] = []
    prefs = [i for i in items if i.get('type') == 'response_preference']
    identities = [i for i in items if i.get('type') == 'identity']
    rules = [i for i in items if i.get('type') == 'work_rule']
    if prefs:
        sections.append('## Preferences\n' + '\n'.join(
            f'- {i["text"]}' for i in prefs))
    if identities:
        sections.append('## About the user\n' + '\n'.join(
            f'- {i["text"]}' for i in identities))
    if rules:
        sections.append('## Work rules\n' + '\n'.join(
            f'- When {i["condition"]} → {i["action"]}' for i in rules))
    return '\n\n'.join(sections).strip()


def _legacy_items(body: str) -> list[dict]:
    """Deterministically migrate the old two-section Markdown profile."""
    rows: list[dict] = []
    header = ''
    stamp = _now()
    for raw in (body or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            header = line.lstrip('#').strip()
            continue
        if line.startswith(('- ', '* ')):
            line = line[2:].strip()
        if not line:
            continue
        normalized = header.casefold()
        item_type = ('response_preference'
                     if not header or normalized == 'preferences'
                     else 'identity')
        rows.append({
            'id': short_id('ctx_', 16),
            'type': item_type,
            'text': line,
            'source': 'legacy_migration',
            'created_at': stamp,
            'updated_at': stamp,
        })
    return rows


def _valid_doc(data) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        return None
    normalized: list[dict] = []
    seen: set[str] = set()
    try:
        for raw in data['items']:
            item = _normalize_item(raw, source_default='manual')
            if item is None:
                continue
            if item['id'] in seen:
                item['id'] = short_id('ctx_', 16)
            seen.add(item['id'])
            normalized.append(item)
    except ContextValidationError as exc:
        logger.warning('[UserContext] invalid context document: %s', exc)
        return None
    return {'version': CONTEXT_VERSION, 'items': normalized}


def _write_legacy_mirror(items: list[dict], scope: str) -> None:
    path = profile_path(scope)
    body = _legacy_markdown(items)
    if not body:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.warning('[UserContext] legacy mirror clear failed: %s', exc)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_text_atomic(path, body + '\n')


def load_context(scope: str = '') -> dict:
    """Load structured context, migrating the legacy Markdown file once."""
    path = context_path(scope)
    data = _valid_doc(read_json(path, default=None))
    if data is not None:
        return data

    legacy = ''
    try:
        with open(profile_path(scope), encoding='utf-8') as handle:
            legacy = handle.read().strip()
    except FileNotFoundError as exc:
        logger.debug('[UserContext] no legacy profile to migrate: %s', exc)
    except OSError as exc:
        logger.warning('[UserContext] legacy profile read failed: %s', exc)

    data = {'version': CONTEXT_VERSION, 'items': _legacy_items(legacy)}
    if legacy:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_atomic(path, data)
        audit_log('user_context_migrated', items=len(data['items']))
    return data


def context_status(scope: str = '') -> dict:
    doc = load_context(scope)
    chars = context_char_count(doc['items'])
    return {
        'version': CONTEXT_VERSION,
        'items': deepcopy(doc['items']),
        'chars': chars,
        'cap': CONTEXT_CHAR_CAP,
        'over_cap': chars > CONTEXT_CHAR_CAP,
    }


def save_context_items(items: list[dict], scope: str = '', *,
                       enforce_cap: bool = True,
                       source_default: str = 'manual') -> dict:
    """Replace all context items after validation and cap enforcement."""
    if not isinstance(items, list):
        raise ContextValidationError('items must be a list')
    stamp = _now()
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in items:
        item = _normalize_item(raw, source_default=source_default, now=stamp)
        if item is None:
            continue
        if item['id'] in seen:
            raise ContextValidationError(f'Duplicate context id: {item["id"]}')
        seen.add(item['id'])
        normalized.append(item)
    chars = context_char_count(normalized)
    if enforce_cap and chars > CONTEXT_CHAR_CAP:
        raise ContextValidationError(
            f'Context exceeds {CONTEXT_CHAR_CAP} character limit')
    doc = {'version': CONTEXT_VERSION, 'items': normalized}
    path = context_path(scope)
    if normalized:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_atomic(path, doc)
    else:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.warning('[UserContext] clear failed: %s', exc)
            raise
    _write_legacy_mirror(normalized, scope)
    audit_log('user_context_saved', items=len(normalized), chars=chars)
    return {'saved': True, 'items': deepcopy(normalized), 'chars': chars,
            'cap': CONTEXT_CHAR_CAP, 'over_cap': chars > CONTEXT_CHAR_CAP}


def _append_change(scope: str, operation: str, before: dict | None,
                   after: dict | None) -> str:
    path = context_changes_path(scope)
    data = read_json(path, default={})
    changes = list(data.get('changes') or []) if isinstance(data, dict) else []
    change_id = short_id('chg_', 16)
    changes.append({
        'id': change_id,
        'operation': operation,
        'item_id': (after or before or {}).get('id', ''),
        'before': deepcopy(before),
        'after': deepcopy(after),
        'created_at': _now(),
        'undone_at': '',
    })
    changes = changes[-CONTEXT_CHANGE_LIMIT:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, {'version': 1, 'changes': changes})
    return change_id


def create_context_item(raw: dict, scope: str = '', *, source: str = 'manual',
                        record_change: bool = False) -> dict:
    doc = load_context(scope)
    item = _normalize_item({**(raw or {}), 'source': source},
                           source_default=source)
    if item is None:
        raise ContextValidationError('Context text is required')
    result = save_context_items(doc['items'] + [item], scope)
    change_id = (_append_change(scope, 'create', None, item)
                 if record_change else '')
    return {'item': deepcopy(item), 'change_id': change_id, **result}


def update_context_item(item_id: str, updates: dict, scope: str = '', *,
                        source: str | None = None,
                        record_change: bool = False) -> dict | None:
    doc = load_context(scope)
    idx = next((i for i, item in enumerate(doc['items'])
                if item.get('id') == item_id), None)
    if idx is None:
        return None
    before = deepcopy(doc['items'][idx])
    merged = {**before, **(updates or {}), 'id': before['id'],
              'created_at': before['created_at'], 'updated_at': _now()}
    if source:
        merged['source'] = source
    after = _normalize_item(merged, source_default=source or before['source'])
    if after is None:
        raise ContextValidationError('Context text is required')
    next_items = list(doc['items'])
    next_items[idx] = after
    result = save_context_items(next_items, scope)
    change_id = (_append_change(scope, 'update', before, after)
                 if record_change else '')
    return {'item': deepcopy(after), 'change_id': change_id, **result}


def delete_context_item(item_id: str, scope: str = '') -> bool:
    doc = load_context(scope)
    remaining = [item for item in doc['items'] if item.get('id') != item_id]
    if len(remaining) == len(doc['items']):
        return False
    save_context_items(remaining, scope)
    return True


def undo_context_change(change_id: str, scope: str = '') -> dict:
    path = context_changes_path(scope)
    data = read_json(path, default={})
    changes = list(data.get('changes') or []) if isinstance(data, dict) else []
    change = next((c for c in changes if c.get('id') == change_id), None)
    if change is None:
        return {'undone': False, 'not_found': True}
    if change.get('undone_at'):
        return {'undone': True, 'already_undone': True}

    doc = load_context(scope)
    current = next((i for i in doc['items']
                    if i.get('id') == change.get('item_id')), None)
    expected = change.get('after')
    if current != expected:
        raise ContextConflictError(
            'This context item changed after it was learned; refresh before undoing')

    if change.get('operation') == 'create':
        next_items = [i for i in doc['items']
                      if i.get('id') != change.get('item_id')]
        restored = None
    elif change.get('operation') == 'update' and change.get('before'):
        restored = deepcopy(change['before'])
        next_items = [restored if i.get('id') == change.get('item_id') else i
                      for i in doc['items']]
    else:
        raise ContextConflictError('This context change cannot be undone')
    result = save_context_items(next_items, scope)
    change['undone_at'] = _now()
    write_json_atomic(path, {'version': 1, 'changes': changes})
    audit_log('user_context_change_undone', change_id=change_id)
    return {'undone': True, 'item': restored, **result}


def legacy_profile_body(scope: str = '') -> str:
    return _legacy_markdown(load_context(scope)['items'])


def legacy_items_to_context(items: list[dict]) -> list[dict]:
    """Translate the old ``[{header,text}]`` API into structured items."""
    stamp = _now()
    out: list[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        text = _clean_text(raw.get('text'))
        if not text:
            continue
        header = _clean_text(raw.get('header'))
        item_type = ('response_preference'
                     if not header or header.casefold() == 'preferences'
                     else 'identity')
        out.append({
            'id': _clean_text(raw.get('id')) or short_id('ctx_', 16),
            'type': item_type,
            'text': text,
            'source': _clean_text(raw.get('source')) or 'manual',
            'created_at': _clean_text(raw.get('created_at')) or stamp,
            'updated_at': stamp,
        })
    return out


__all__ = [
    'CONTEXT_VERSION', 'CONTEXT_TYPES', 'CONTEXT_CHAR_CAP',
    'ContextValidationError', 'ContextConflictError', 'load_context',
    'context_status', 'context_markdown', 'context_char_count',
    'save_context_items', 'create_context_item', 'update_context_item',
    'delete_context_item', 'undo_context_change', 'legacy_profile_body',
    'legacy_items_to_context',
]
