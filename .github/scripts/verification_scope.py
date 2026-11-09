#!/usr/bin/env python3
"""Choose CI qualification scope from its event and checked-out Git history.

Entry points: choose_scope and main (GitHub event environment). Only stdlib and
Git; no application import, secrets or network. Missing comparison history
requires full qualification. docs/TESTING_STRATEGY.md owns the delivery policy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def _git(root, *arguments):
    return subprocess.run(['git', *arguments], cwd=root, check=False,
                          capture_output=True, text=True)


def choose_scope(root, event_name, event):
    if not isinstance(event, dict):
        raise ValueError('the CI event must be an object')
    if event_name not in {'push', 'pull_request', 'schedule', 'workflow_dispatch'}:
        raise ValueError(f'unsupported CI event: {event_name}')
    candidate = _git(root, 'rev-parse', '--verify', 'HEAD')
    if candidate.returncode:
        raise ValueError('CI has no checked-out candidate revision')
    result = {'profile': 'full', 'head': candidate.stdout.strip(), 'base': '',
              'reason': 'qualification event'}
    if event_name in {'schedule', 'workflow_dispatch'} or str(event.get('ref', '')).startswith('refs/tags/'):
        return result
    if event_name == 'pull_request':
        pull_request = event.get('pull_request') or {}
        if not isinstance(pull_request, dict) or not isinstance(pull_request.get('base', {}), dict):
            raise ValueError('invalid pull-request comparison evidence')
        base = pull_request.get('base', {}).get('sha', '')
    else:
        base = event.get('before', '')
    result['reason'] = 'comparison history unavailable; full qualification required'
    if not isinstance(base, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', base):
        return result
    compared = _git(root, 'merge-base', base, result['head'])
    if compared.returncode or not compared.stdout.strip():
        return result
    result.update(profile='incremental', base=compared.stdout.strip(),
                  reason='verify the candidate against its comparison revision')
    return result


def main():
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        result = choose_scope(Path.cwd(), os.environ['GITHUB_EVENT_NAME'], event)
        if output := os.environ.get('GITHUB_OUTPUT'):
            with open(output, 'a', encoding='utf-8') as handle:
                for key in ('profile', 'head', 'base'):
                    handle.write(f'{key}={result[key]}\n')
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'CI verification scope unavailable: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
