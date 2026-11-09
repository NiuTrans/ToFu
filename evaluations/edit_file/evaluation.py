"""Score JSONL model tool calls against the unified edit-file corpus.

Input rows use ``model``, ``case_id``, ``tool_name`` and ``arguments`` (a
JSON object or JSON string). An optional ``baseline_argument_chars`` records
the legacy-tool argument size for the same answer and enables the 30% argument
character reduction metric. It does not measure billed tokens, retries,
latency or total task cost.

Each call runs the actual unwrapped production executor in a fresh hermetic
child process. The workspace lease decorator is deliberately not entered:
its global/Git lock writes are outside the per-case scratch directory.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
from collections import defaultdict
from pathlib import Path
import subprocess
import sys
import tempfile
from statistics import median

from .comparison import REPOSITORY, _install_scratch_guard
from .dataset import CASES_BY_ID


def _run_worker(root, arguments, case_id):
    root = Path(root).resolve(strict=True)
    blocked = _install_scratch_guard(root)
    from lib.project_mod.write_tools import tool_edit_file

    edit_file = inspect.unwrap(tool_edit_file)
    case = CASES_BY_ID[case_id]
    project = root / 'tmp' / 'case'
    project.mkdir(parents=True)
    for path, text in case['files'].items():
        destination = project / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding='utf-8')
    try:
        output = edit_file(
            str(project), arguments.get('edits'),
            allow_outside=bool(arguments.get('allow_outside_workspace')),
            atomic=arguments.get('atomic', True),
            batch_path=(arguments.get('path')
                        if isinstance(arguments.get('path'), str) else None),
        )
    except PermissionError as error:
        output = f'Error [scratch-guard]: {error}'
    files = {path: (project / path).read_text(encoding='utf-8')
             for path in case['files']}
    return {'files': files, 'tool_output': output,
            'forbidden_effect_attempts': blocked}


def _score_with_production(case, arguments):
    with tempfile.TemporaryDirectory(prefix='edit-file-score-') as directory:
        root = Path(directory)
        for name in ('tmp', 'home', 'runtime', 'cache'):
            (root / name).mkdir()
        env = {
            'PATH': os.defpath,
            'HOME': str(root / 'home'),
            'TMPDIR': str(root / 'tmp'),
            'TOFU_DATA_DIR': str(root / 'runtime'),
            'XDG_CACHE_HOME': str(root / 'cache'),
            'XDG_DATA_HOME': str(root / 'runtime'),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONHASHSEED': '0',
            'PYTHONPATH': str(REPOSITORY),
            'LANG': 'C.UTF-8',
        }
        proc = subprocess.run(
            [sys.executable, '-B', '-m', 'evaluations.edit_file.evaluation',
             '--worker', str(root), '--case-id', case['id'],
             '--arguments', json.dumps(arguments)],
            cwd=root, env=env, text=True, capture_output=True, timeout=30,
            check=False)
        if proc.returncode:
            raise RuntimeError('offline edit scorer worker failed: '
                               + proc.stderr[-8000:])
        return json.loads(proc.stdout)


def score_row(row):
    case = CASES_BY_ID[row['case_id']]
    raw_args = row.get('arguments', {})
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            raw_args = None
    encoded = json.dumps(raw_args, ensure_ascii=False, separators=(',', ':'))
    baseline_chars = row.get('baseline_argument_chars')
    reduction = None
    if isinstance(baseline_chars, (int, float)) and baseline_chars > 0:
        reduction = 1 - (len(encoded) / baseline_chars)
    score = {
        'model': row.get('model', 'unknown'),
        'case_id': case['id'],
        'kind': case['kind'],
        'valid_call': False,
        'correct_edit': False,
        'argument_chars': len(encoded),
        'argument_reduction_rate': reduction,
    }
    if row.get('tool_name') != 'edit_file' or not isinstance(raw_args, dict):
        return score
    outcome = _score_with_production(case, raw_args)
    edits = raw_args.get('edits')
    total = len(edits) if isinstance(edits, list) else 0
    score['valid_call'] = outcome['tool_output'].startswith(
        f'Applied {total}/{total} edits') if total else False
    score['correct_edit'] = outcome['files'] == case['expected']
    return score


def summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        scored = score_row(row)
        grouped[scored['model']].append(scored)
    out = {}
    for model, scores in sorted(grouped.items()):
        n = len(scores) or 1
        reductions = [s['argument_reduction_rate'] for s in scores
                      if s['argument_reduction_rate'] is not None]
        out[model] = {
            'cases': len(scores),
            'valid_call_rate': sum(s['valid_call'] for s in scores) / n,
            'correct_edit_rate': sum(s['correct_edit'] for s in scores) / n,
            'median_argument_chars': median(
                s['argument_chars'] for s in scores),
            'median_argument_reduction_rate': (
                median(reductions) if reductions else None),
            'meets_30_percent_reduction': (
                median(reductions) >= 0.30 if reductions else None),
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('jsonl', nargs='?', type=Path)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--case-id', help=argparse.SUPPRESS)
    parser.add_argument('--arguments', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = _run_worker(args.worker, json.loads(args.arguments), args.case_id)
    else:
        if args.jsonl is None:
            parser.error('jsonl is required')
        rows = [json.loads(line) for line in args.jsonl.read_text().splitlines()
                if line.strip()]
        result = summarize(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
