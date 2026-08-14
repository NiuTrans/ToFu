#!/usr/bin/env python3
"""Opt-in live smoke for OpenAI Programmatic Tool Calling.

No credential is read or transmitted in ``--dry-run`` mode.  The live path
uses a deterministic, side-effect-free in-memory function and validates the
full stateless continuation contract: program item, caller linkage, bounded
child calls, program_output, and final assistant message.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ``python scripts/ptc_live_smoke.py`` puts scripts/, not the repository root,
# on sys.path. Resolve the local application contract without installation.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.tools.programmatic import (
    PROGRAMMATIC_MAX_CALLS,
    PROGRAMMATIC_MAX_CONTINUATIONS,
)


_SCORES = {'alpha': 9, 'beta': 4, 'gamma': 8, 'delta': 2}


def _tools() -> list[dict[str, Any]]:
    return [{
        'type': 'function',
        'name': 'lookup_score',
        'description': 'Return id (string) and score (integer) for one id.',
        'parameters': {
            'type': 'object',
            'properties': {'id': {'type': 'string'}},
            'required': ['id'],
            'additionalProperties': False,
        },
        'output_schema': {
            'type': 'object',
            'properties': {
                'id': {'type': 'string'},
                'score': {'type': 'integer'},
            },
            'required': ['id', 'score'],
            'additionalProperties': False,
        },
        'allowed_callers': ['programmatic'],
        'strict': True,
    }, {'type': 'programmatic_tool_calling'}]


def _initial_input() -> list[dict[str, Any]]:
    return [{
        'role': 'user',
        'content': (
            'Use Programmatic Tool Calling with lookup_score for exactly '
            'alpha, beta, gamma, and delta. Run independent lookups '
            'concurrently, keep records whose score is at least 8, sort by id, '
            'and emit exactly one JSON object '
            '{"selected":[{"id":string,"score":integer}]}. Then give a '
            'brief final answer containing both selected ids. Do not call any '
            'tool directly and do not retry.'),
    }]


def _post(url: str, api_key: str, payload: dict[str, Any],
          timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        })
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:1000]
        raise RuntimeError(f'OpenAI HTTP {exc.code}: {detail}') from exc


def _message_text(item: dict[str, Any]) -> str:
    parts = item.get('content') or []
    return ''.join(
        str(part.get('text') or part.get('refusal') or '')
        for part in parts if isinstance(part, dict))


def run_smoke(*, api_key: str, model: str, base_url: str,
              timeout_s: float) -> dict[str, Any]:
    input_items = _initial_input()
    tools = _tools()
    saw_program = False
    saw_program_output = False
    call_count = 0
    response_rounds = 0
    final_text = ''
    program_result: Any = None

    # One initial request plus the exact application continuation ceiling.
    for _ in range(PROGRAMMATIC_MAX_CONTINUATIONS + 1):
        response_rounds += 1
        response = _post(
            base_url.rstrip('/') + '/responses', api_key, {
                'model': model,
                'store': False,
                'input': input_items,
                'tools': tools,
            }, timeout_s)
        if response.get('status') != 'completed':
            raise RuntimeError(
                f"response status={response.get('status')!r}: "
                f"{response.get('error') or response.get('incomplete_details')}")
        output = [item for item in response.get('output') or []
                  if isinstance(item, dict)]
        # Stateless protocol: replay every output item verbatim.
        input_items.extend(output)
        for item in output:
            if item.get('type') == 'program':
                saw_program = True
            elif item.get('type') == 'program_output':
                saw_program_output = True
                raw = item.get('result')
                try:
                    program_result = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    program_result = raw

        calls = [item for item in output
                 if item.get('type') == 'function_call']
        if calls:
            if call_count + len(calls) > PROGRAMMATIC_MAX_CALLS:
                raise RuntimeError('program exceeded the application call budget')
            call_outputs = []
            for call in calls:
                caller = call.get('caller')
                if (not isinstance(caller, dict)
                        or caller.get('type') != 'program'
                        or not caller.get('caller_id')):
                    raise RuntimeError('child call lost its program caller linkage')
                if call.get('name') != 'lookup_score':
                    raise RuntimeError(f"unexpected tool {call.get('name')!r}")
                args = json.loads(call.get('arguments') or '{}')
                record_id = str(args.get('id') or '')
                if record_id not in _SCORES:
                    raise RuntimeError(f'unexpected lookup id {record_id!r}')
                result = {'id': record_id, 'score': _SCORES[record_id]}
                call_outputs.append({
                    'type': 'function_call_output',
                    'call_id': call.get('call_id'),
                    'output': json.dumps(result),
                    'caller': dict(caller),
                })
            call_count += len(calls)
            input_items.extend(call_outputs)
            continue

        message = next((item for item in output
                        if item.get('type') == 'message'), None)
        if message:
            final_text = _message_text(message)
            break
    else:
        raise RuntimeError('no final message within the continuation budget')

    selected = (program_result or {}).get('selected') \
        if isinstance(program_result, dict) else None
    selected_ids = sorted(
        str(row.get('id')) for row in (selected or []) if isinstance(row, dict))
    expected = ['alpha', 'gamma']
    if not saw_program or not saw_program_output:
        raise RuntimeError('provider did not produce the required PTC items')
    if selected_ids != expected:
        raise RuntimeError(
            f'program result mismatch: expected {expected}, got {selected_ids}')
    if not all(record_id in final_text for record_id in expected):
        raise RuntimeError('final assistant message omitted selected evidence')
    return {
        'ok': True,
        'model': model,
        'responseRounds': response_rounds,
        'childCalls': call_count,
        'sawProgram': saw_program,
        'sawProgramOutput': saw_program_output,
        'selectedIds': selected_ids,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='gpt-5.6')
    parser.add_argument('--base-url', default=os.environ.get(
        'OPENAI_BASE_URL', 'https://api.openai.com/v1'))
    parser.add_argument('--timeout', type=float, default=120.0)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps({
            'model': args.model,
            'store': False,
            'input': _initial_input(),
            'tools': _tools(),
            'maxCalls': PROGRAMMATIC_MAX_CALLS,
            'maxContinuations': PROGRAMMATIC_MAX_CONTINUATIONS,
        }, ensure_ascii=False, indent=2))
        return 0
    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        print('OPENAI_API_KEY is not configured; live smoke not run.',
              file=sys.stderr)
        return 2
    try:
        result = run_smoke(
            api_key=api_key, model=args.model, base_url=args.base_url,
            timeout_s=args.timeout)
    except Exception as exc:
        print(f'PTC live smoke failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
