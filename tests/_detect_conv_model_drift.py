#!/usr/bin/env python3
"""Detect conversations whose stored ``settings.model`` disagrees with the
model their turns actually ran on — READ-ONLY, never writes.

WHY
---
The 2026-07-27 composer bug (conv ms352oniikgq10) let a paint-time default be
laundered into ``settings.model`` via the tool-state write-back. The code
paths are fixed (see tests/test_frontend_conv_model_identity.py), but rows
already written keep the wrong value.

DELIBERATELY NOT AUTO-REPAIRED (owner decision, 2026-07-27): "stored model !=
last turn's model" is ALSO the legitimate shape of a user who switched models
mid-conversation, so rewriting it would silently overwrite a real choice to
tidy up our own bug. This script only REPORTS; a human decides per case.

CLASSIFICATION
--------------
  fallback   — the last assistant turn carries fallbackModel/fallbackFrom/
               fallbackReason. A genuine runtime fallback; stored model is
               correct and the turn simply ran elsewhere. NOT corruption.
  reverse    — stored model equals a known FALLBACK TARGET while the turns ran
               a different (typically stronger) model. A fallback can only
               move AWAY from the configured model, never rewrite settings TO
               the target, so this shape cannot be produced legitimately.
               Strongest corruption signal.
  suspicious — mismatch with no fallback marker. Either the bug, or a genuine
               mid-conversation model switch. Needs a human read.

USAGE
    python tests/_detect_conv_model_drift.py [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))

from lib.database import DOMAIN_CHAT, get_thread_db  # noqa: E402


def _fallback_targets() -> set:
    """The configured fallback/default models — a stored value equal to one of
    these on a conv that ran something else is the reverse-direction signal."""
    targets = set()
    try:
        cfg_path = os.path.join(os.path.dirname(__file__), '..',
                                'data', 'config', 'server_config.json')
        with open(cfg_path, encoding='utf-8') as f:
            defaults = (json.load(f).get('model_defaults') or {})
        for key in ('fallback_model', 'default_model'):
            if defaults.get(key):
                targets.add(defaults[key])
    except (OSError, ValueError) as e:
        print(f'[warn] could not read model_defaults: {e}', file=sys.stderr)
    return targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=300,
                    help='how many recent conversations to scan')
    args = ap.parse_args()

    targets = _fallback_targets()
    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        'SELECT id, title, settings, messages FROM conversations '
        'WHERE user_id=1 ORDER BY updated_at DESC LIMIT ?',
        (args.limit,)).fetchall()

    buckets: dict = {'reverse': [], 'suspicious': [], 'fallback': []}
    scanned = 0
    for r in rows:
        try:
            settings = json.loads(r[2] or '{}')
            msgs = json.loads(r[3] or '[]')
        except (ValueError, TypeError):
            continue
        scanned += 1
        stored = settings.get('model')
        if not stored:
            continue
        last = None
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('model'):
                last = m
                break
        if not last or last.get('model') == stored:
            continue

        entry = (r[0], (r[1] or '')[:48], stored, last.get('model'))
        if last.get('fallbackModel') or last.get('fallbackFrom') or last.get('fallbackReason'):
            buckets['fallback'].append(entry)
        elif stored in targets:
            buckets['reverse'].append(entry)
        else:
            buckets['suspicious'].append(entry)

    print(f'scanned {scanned} conversations; fallback targets = {sorted(targets)}\n')
    for name, label in (
        ('reverse', 'REVERSE-DIRECTION — cannot be a legitimate fallback'),
        ('suspicious', 'SUSPICIOUS — no fallback marker; human call'),
        ('fallback', 'GENUINE FALLBACK — stored model is correct, no action'),
    ):
        rowset = buckets[name]
        print(f'== {label} ({len(rowset)}) ==')
        for cid, title, stored, ran in rowset:
            print(f'   {cid}  stored={stored}  ran={ran}  |  {title}')
        print()
    print('READ-ONLY: nothing was modified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
