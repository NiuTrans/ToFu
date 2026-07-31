#!/usr/bin/env python3
"""tests/_migrate_model_identity.py — migrate server_config.json onto the
model-identity contract (lib/llm_dispatch/model_entry.py).

Rewrites each provider model entry from the pre-contract shape

    {"model_id": "aws.claude-opus-4.8", "aliases": ["yuju-claude-opus-4.8-evaDaily"]}

to the contract shape

    {"model_id": "claude-opus-4.8",
     "request_ids": ["aws.claude-opus-4.8", "yuju-claude-opus-4.8-evaDaily"]}

``model_id`` becomes the LOGICAL name (what presets target and the picker
shows); ``request_ids`` is the ordered pool of ids actually sent on the wire.

Safety properties
-----------------
* **Dry-run by default.** Pass ``--apply`` to write. A timestamped backup of
  the config is taken before the first write.
* **Idempotent.** An entry that already declares ``request_ids`` is left alone,
  so re-running is a no-op.
* **Never loses a wire id.** The new pool is exactly the old resolved pool
  (``[model_id] + aliases``). This is the property that matters: dropping one
  deployment is silent because the remaining ids still answer.
* **Presets follow the rename.** A preset pointing at a renamed ``model_id`` is
  repointed, otherwise the user's chosen model silently becomes unset.
* Entries whose id is already logical (no provider taint) are left untouched.

Usage:
    python3 tests/_migrate_model_identity.py            # dry run (default)
    python3 tests/_migrate_model_identity.py --apply
    python3 tests/_migrate_model_identity.py --config path/to/server_config.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A gateway/deployment prefix or suffix — the id names WHERE a model is served,
# not WHICH model it is. Kept in sync with tests/test_model_entry_contract.py.
_PREFIX = re.compile(r'^(aws\.|vertex\.|azure\.|bedrock\.)', re.IGNORECASE)
_YUJU = re.compile(r'^yuju-(.+?)-evaDaily$', re.IGNORECASE)
_SUFFIX = re.compile(r'(-huawei|-tencent|-baidu|-doubao|-nova\d+|-b)$', re.IGNORECASE)


def logical_name(model_id: str) -> str:
    """Derive the clean logical name from a deployment-tainted id.

    Returns *model_id* unchanged when it carries no taint.
    """
    m = _YUJU.match(model_id or '')
    if m:
        return m.group(1)
    out = _PREFIX.sub('', model_id or '')
    out = _SUFFIX.sub('', out)
    return out or model_id


def plan_entry(entry: dict) -> dict | None:
    """Return ``{'from', 'to', 'request_ids'}`` when *entry* needs migrating."""
    if not isinstance(entry, dict):
        return None
    mid = (entry.get('model_id') or '').strip()
    if not mid:
        return None
    if entry.get('request_ids'):
        return None                      # already on the contract
    aliases = [a for a in (entry.get('aliases') or []) if a]
    pool = [mid] + [a for a in aliases if a != mid]
    new_id = logical_name(mid)
    if new_id == mid and not aliases:
        return None                      # already logical, single deployment
    return {'from': mid, 'to': new_id, 'request_ids': pool}


def migrate(cfg: dict) -> tuple[dict, list]:
    """Apply the migration to *cfg* in place. Returns (cfg, change log)."""
    changes: list[str] = []
    renames: dict[str, str] = {}

    for prov in cfg.get('providers') or []:
        for entry in prov.get('models') or []:
            plan = plan_entry(entry)
            if not plan:
                continue
            entry['model_id'] = plan['to']
            entry['request_ids'] = plan['request_ids']
            entry.pop('aliases', None)   # routing now lives in request_ids only
            if plan['from'] != plan['to']:
                renames[plan['from']] = plan['to']
            changes.append('%s: %r -> model_id=%r request_ids=%r'
                           % (prov.get('id', '?'), plan['from'], plan['to'],
                              plan['request_ids']))

    # Presets / model defaults store a model_id — carry them across the rename
    # or the user's selected model silently becomes unset.
    for section in ('presets', 'models', 'model_defaults'):
        blob = cfg.get(section)
        if not isinstance(blob, dict):
            continue
        for k, v in list(blob.items()):
            if isinstance(v, str) and v in renames:
                blob[k] = renames[v]
                changes.append('%s.%s: %r -> %r' % (section, k, v, renames[v]))
    return cfg, changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='write the changes (default: dry run)')
    ap.add_argument('--config', default='')
    args = ap.parse_args()

    path = args.config
    if not path:
        from lib.config_dir import config_path
        path = config_path('server_config.json')
    if not os.path.isfile(path):
        print('No config at %s — nothing to do.' % path)
        return 0

    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)

    before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    cfg, changes = migrate(cfg)
    after = json.dumps(cfg, ensure_ascii=False, sort_keys=True)

    if not changes or before == after:
        print('Already on the model-identity contract — no changes. (%s)' % path)
        return 0

    print('%d change(s) for %s:' % (len(changes), path))
    for c in changes:
        print('  ' + c)

    if not args.apply:
        print('\nDRY RUN — re-run with --apply to write.')
        return 0

    backup = '%s.bak-%s' % (path, time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, backup)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    print('\nApplied. Backup: %s' % backup)
    print('Restart the server (or save Settings) for the dispatcher to rebuild.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
