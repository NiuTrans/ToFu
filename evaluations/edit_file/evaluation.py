"""Score JSONL model tool calls against the unified edit-file corpus.

Input rows use ``model``, ``case_id``, ``tool_name`` and ``arguments`` (a
JSON object or JSON string). An optional ``baseline_argument_chars`` records
the legacy-tool argument size for the same answer and enables the 30% token
reduction gate. The scorer is provider-neutral and performs no network calls,
so the same captured outputs can be compared reproducibly.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

from .dataset import CASES_BY_ID


def _apply(files, edits):
    current = dict(files)
    operations = []
    for edit in edits:
        path = edit['path']
        operation = edit['operation']
        anchor = edit['anchor']
        content = edit['content']
        text = current[path]
        count = text.count(anchor)
        if not count or (count > 1 and not (
                operation == 'replace' and edit.get('replace_all'))):
            raise ValueError('anchor cardinality')
        if operation == 'replace':
            text = (text.replace(anchor, content) if edit.get('replace_all')
                    else text.replace(anchor, content, 1))
        elif operation == 'insert_before':
            inserted = content if content.endswith('\n') else content + '\n'
            text = text.replace(anchor, inserted + anchor, 1)
        elif operation == 'insert_after':
            idx = text.index(anchor) + len(anchor)
            inserted = content
            if idx < len(text) and text[idx] == '\n':
                idx += 1
            elif idx < len(text):
                inserted = '\n' + inserted
            if not inserted.endswith('\n'):
                inserted += '\n'
            text = text[:idx] + inserted + text[idx:]
        else:
            raise ValueError('operation')
        current[path] = text
        operations.append(operation)
    return current, operations


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
        'efficient_operation': False,
        'argument_chars': len(encoded),
        'argument_reduction_rate': reduction,
    }
    if row.get('tool_name') != 'edit_file' or not isinstance(raw_args, dict):
        return score
    edits = raw_args.get('edits')
    if not isinstance(edits, list) or not edits:
        return score
    try:
        output, operations = _apply(case['files'], edits)
    except (KeyError, TypeError, ValueError):
        return score
    score['valid_call'] = True
    score['correct_edit'] = output == case['expected']
    score['efficient_operation'] = all(
        operation in case['expected_operations'] for operation in operations)
    return score


def summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        scored = score_row(row)
        grouped[scored['model']].append(scored)
    out = {}
    for model, scores in sorted(grouped.items()):
        n = len(scores) or 1
        additive = [s for s in scores if s['kind'] == 'additive']
        reductions = [s['argument_reduction_rate'] for s in scores
                      if s['argument_reduction_rate'] is not None]
        out[model] = {
            'cases': len(scores),
            'valid_call_rate': sum(s['valid_call'] for s in scores) / n,
            'correct_edit_rate': sum(s['correct_edit'] for s in scores) / n,
            'additive_insert_rate': (
                sum(s['efficient_operation'] for s in additive) / len(additive)
                if additive else None),
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
    parser.add_argument('jsonl', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.jsonl.read_text().splitlines()
            if line.strip()]
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
