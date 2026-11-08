"""Build the frozen task set from task_specs.py and the pinned public sources.

Usage: python3 build_tasks.py            # writes tasks/tasks.jsonl, tasks/files/*
Refuses a task whose derived stratum differs from the declared one, whose
span is ambiguous, or whose source checkout is not at the pinned commit.
"""
import hashlib
import json
import os
import subprocess
import sys

import tiktoken

sys.path.insert(0, os.path.dirname(__file__))
from task_specs import FILES, REPOS, TASKS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENC = tiktoken.get_encoding('o200k_base')
RANK = {'S': 0, 'M': 1, 'L': 2}


def ntok(text):
    return len(ENC.encode(text))


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_source(key):
    repo, path = FILES[key]
    url, commit = REPOS[repo]
    checkout = os.path.join(ROOT, 'repos', repo)
    head = subprocess.run(['git', '-C', checkout, 'rev-parse', 'HEAD'], text=True,
                          capture_output=True, check=True).stdout.strip()
    if head != commit:
        raise SystemExit(f'{repo} checkout at {head}, pinned {commit}')
    with open(os.path.join(checkout, path), encoding='utf-8', newline='') as f:
        text = f.read()
    if '\r' in text or not text.endswith('\n'):
        raise SystemExit(f'{path}: expected LF text with a final newline')
    return text


def whole_line_count(lines, line):
    return sum(1 for item in lines if item == line)


def site_stratum(lines, site):
    kind = site['kind']
    if kind == 'ins':
        adj = lines[site['line'] - 1]
        count = whole_line_count(lines, adj)
        if not adj.strip() or count > 1:
            return 'L', {'adjacent': adj, 'adjacent_whole_line_count': count,
                         'adjacent_tokens': ntok(adj.strip())}
        return ('S' if ntok(adj.strip()) <= 2 else 'M'), {
            'adjacent': adj, 'adjacent_whole_line_count': count,
            'adjacent_tokens': ntok(adj.strip())}
    if kind in ('rep_span', 'del_span'):
        line = lines[site['line'] - 1]
        if line.count(site['old']) != 1:
            raise SystemExit(f'span {site["old"]!r} not unique in line {site["line"]}')
        return ('S' if ntok(site['old']) <= 2 else 'M'), {
            'old_tokens': ntok(site['old']), 'old_file_count': '\n'.join(lines).count(site['old'])}
    span = lines[site['start'] - 1:site['end']]
    if site['end'] - site['start'] >= 1:
        return 'L', {'old_lines': len(span), 'old_tokens': ntok('\n'.join(span))}
    tokens = ntok(span[0].strip())
    return ('S' if tokens <= 2 else 'M'), {'old_lines': 1, 'old_tokens': tokens}


def apply_sites(text, sites):
    lines = text[:-1].split('\n')
    keyed = []
    for site in sites:
        start = site.get('line', site.get('start'))
        keyed.append((start, site))
    for _start, site in sorted(keyed, key=lambda item: -item[0]):
        kind = site['kind']
        if kind == 'ins':
            new = site['text'][:-1].split('\n')
            at = site['line'] if site['position'] == 'after' else site['line'] - 1
            lines[at:at] = new
        elif kind == 'rep_span':
            i = site['line'] - 1
            lines[i] = lines[i].replace(site['old'], site['new'], 1)
        elif kind == 'del_span':
            i = site['line'] - 1
            lines[i] = lines[i].replace(site['old'], '', 1)
        elif kind == 'rep_lines':
            lines[site['start'] - 1:site['end']] = site['text'][:-1].split('\n')
        elif kind == 'del_lines':
            del lines[site['start'] - 1:site['end']]
    return '\n'.join(lines) + '\n'


def fence(text):
    body = text if text.endswith('\n') else text + '\n'
    return '```\n' + body + '```'


def quote(line):
    return '`' + line.strip() + '`'


def render_site(lines, site):
    kind = site['kind']
    if kind == 'ins':
        n = site['line']
        count = len(site['text'][:-1].split('\n'))
        what = 'line' if count == 1 else f'{count} lines'
        where = 'immediately after' if site['position'] == 'after' else 'immediately before'
        ref = quote(lines[n - 1]) if lines[n - 1].strip() else '(a blank line)'
        return (f'Insert the following {what} {where} line {n} ({ref}), as new line(s), '
                f'with exactly this indentation:\n{fence(site["text"])}')
    if kind == 'rep_span':
        n = site['line']
        after = lines[n - 1].replace(site['old'], site['new'], 1)
        return (f'On line {n}, change `{site["old"]}` to `{site["new"]}`. '
                f'The whole line must become:\n{fence(after)}')
    if kind == 'del_span':
        n = site['line']
        after = lines[n - 1].replace(site['old'], '', 1)
        return (f'On line {n}, delete `{site["old"]}`. The whole line must become:\n{fence(after)}')
    if kind == 'rep_lines':
        a, b = site['start'], site['end']
        head = f'line {a} ({quote(lines[a - 1])})' if a == b else (
            f'lines {a}-{b} (from {quote(lines[a - 1])} through {quote(lines[b - 1])})')
        return f'Replace {head} with exactly:\n{fence(site["text"])}'
    a, b = site['start'], site['end']
    if a == b:
        return f'Delete line {a} ({quote(lines[a - 1])}) entirely, including its line break.'
    return (f'Delete lines {a}-{b} (from {quote(lines[a - 1])} through {quote(lines[b - 1])}) '
            'entirely, including their line breaks.')


def render_instruction(path, lines, sites):
    if len(sites) == 1:
        body = render_site(lines, sites[0])
        return (f'Edit `{path}`. {body}\n'
                'Line numbers refer to the file as shown above. Keep everything else in '
                'the file byte-for-byte unchanged.')
    items = '\n'.join(f'{i}. {render_site(lines, s)}' for i, s in enumerate(sites, 1))
    return (f'Edit `{path}`: make all {len(sites)} of the following edits.\n{items}\n'
            'Line numbers refer to the file as shown above (before any of these edits). '
            'Keep everything else in the file byte-for-byte unchanged.')


def main():
    out_dir = os.path.join(ROOT, 'tasks')
    files_dir = os.path.join(out_dir, 'files')
    os.makedirs(files_dir, exist_ok=True)
    sources = {key: load_source(key) for key in FILES}
    for key, text in sources.items():
        with open(os.path.join(files_dir, f'{key}.orig'), 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    rows = []
    cells = {}
    problems = []
    for task_id, key, edit_type, declared, sites in TASKS:
        repo, path = FILES[key]
        text = sources[key]
        lines = text[:-1].split('\n')
        strata, metrics = [], []
        for site in sites:
            stratum, metric = site_stratum(lines, site)
            strata.append(stratum)
            metrics.append(metric)
        derived = max(strata, key=RANK.get)
        if derived != declared:
            problems.append(f'{task_id}: declared {declared}, derived {derived} {metrics}')
            continue
        if edit_type == 'insert' and not (len(sites) == 1 and sites[0]['kind'] == 'ins'):
            raise SystemExit(f'{task_id}: insert task must be one pure insertion')
        if edit_type == 'multi' and len(sites) < 2:
            raise SystemExit(f'{task_id}: multi task needs 2+ sites')
        expected = apply_sites(text, sites)
        if expected == text:
            raise SystemExit(f'{task_id}: no-op')
        with open(os.path.join(files_dir, f'{task_id}.expected'), 'w', encoding='utf-8', newline='') as f:
            f.write(expected)
        has_insert = any(s['kind'] == 'ins' for s in sites)
        rows.append({
            'id': task_id, 'file_key': key, 'repo': repo, 'repo_url': REPOS[repo][0],
            'commit': REPOS[repo][1], 'path': path, 'edit_type': edit_type, 'stratum': declared,
            'position': sites[0].get('position') if edit_type == 'insert' else None,
            'has_insertion_site': has_insert, 'n_sites': len(sites), 'sites': sites,
            'site_strata': strata, 'site_metrics': metrics,
            'source_sha256': sha(text), 'expected_sha256': sha(expected),
            'source_lines': len(lines), 'source_tokens': ntok(text),
            'instruction': render_instruction(path, lines, sites),
        })
        cells[(edit_type, declared)] = cells.get((edit_type, declared), 0) + 1
    if problems:
        raise SystemExit('\n'.join(problems))
    with open(os.path.join(out_dir, 'tasks.jsonl'), 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(f'{len(rows)} tasks')
    for edit_type in ('insert', 'replace', 'delete', 'multi'):
        print(edit_type, {s: cells.get((edit_type, s), 0) for s in 'SML'})
    positions = {}
    for row in rows:
        if row['position']:
            positions[row['position']] = positions.get(row['position'], 0) + 1
    print('insert positions', positions)


if __name__ == '__main__':
    main()
