"""Build Experiment 2 (write_file modes) tasks: tasks/write_tasks.jsonl + tasks/files/W*.{orig,expected}.

E2a append vs full rewrite: synthetic but realistic growing logs (markdown run log,
CSV metrics log, service log) at ~40/200/800 lines; append a given block.
E2b content_ref vs inline: a previous `run_command` round printed an output; save the
program output (whole, or the section between marker lines) to a new file.
E2c large-file mid-edit: six public source files of 555-1,050 lines; insert one
comment line after a unique line near the middle (full rewrite vs edit_file).
Deterministic (seeded); no internal data.
"""
import hashlib
import json
import os
import random
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = os.path.join(ROOT, 'tasks', 'files')
RNG = random.Random(20261106)


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def md_log(n, start=1):
    out = ['# Benchmark run log', '', 'One entry per evaluation run, oldest first.', '']
    for i in range(start, start + n):
        passed = RNG.randint(18, 38)
        out += [f'## Run {i:04d} - 2026-10-{(i % 28) + 1:02d}', '',
                f'- dataset: tb-subset-{RNG.choice([40, 60, 80])}',
                f'- model: {RNG.choice(["model-a", "model-b", "model-c"])}',
                f'- pass@1: {passed / 40:.3f} ({passed}/40)',
                f'- median wall time: {RNG.randint(180, 900)} s', '']
    return '\n'.join(out) + '\n'


def md_entry(i):
    return (f'## Run {i:04d} - 2026-11-06\n\n- dataset: tb-subset-60\n- model: model-b\n'
            f'- pass@1: 0.717 (43/60)\n- median wall time: 388 s\n- notes: first run after the sandbox image refresh\n\n')


def csv_log(n):
    out = ['timestamp,run,task,status,seconds,tokens']
    for i in range(n):
        out.append(f'2026-10-{(i % 28) + 1:02d}T{i % 24:02d}:{(i * 7) % 60:02d}:00Z,run-{i // 20 + 1:04d},'
                   f'task-{RNG.randint(1, 120):03d},{RNG.choice(["pass", "pass", "fail", "timeout"])},'
                   f'{RNG.uniform(20, 900):.1f},{RNG.randint(4000, 90000)}')
    return '\n'.join(out) + '\n'


def csv_rows():
    return ''.join(f'2026-11-06T10:{m:02d}:00Z,run-0099,task-{t:03d},{s},{sec},{tok}\n' for m, t, s, sec, tok in [
        (0, 7, 'pass', '212.4', 18234), (4, 19, 'fail', '640.0', 71200), (9, 33, 'pass', '98.7', 9912),
        (13, 58, 'timeout', '900.0', 88001), (21, 77, 'pass', '305.1', 24420), (26, 101, 'pass', '187.9', 15003)])


def svc_log(n):
    out = []
    for i in range(n):
        out.append(f'2026-10-30 {i % 24:02d}:{(i * 13) % 60:02d}:{(i * 29) % 60:02d}.{i % 1000:03d} '
                   f'{RNG.choice(["INFO ", "INFO ", "WARN ", "DEBUG"])} worker-{RNG.randint(1, 8)} '
                   f'request id={RNG.getrandbits(32):08x} path=/api/{RNG.choice(["jobs", "runs", "tasks", "logs"])} '
                   f'status={RNG.choice([200, 200, 200, 404, 500])} ms={RNG.randint(2, 900)}')
    return '\n'.join(out) + '\n'


def svc_lines():
    return ('2026-11-06 10:00:01.004 INFO  worker-2 request id=0badc0de path=/api/runs status=200 ms=14\n'
            '2026-11-06 10:00:01.377 WARN  worker-5 request id=1ce1ce00 path=/api/jobs status=429 ms=3\n'
            '2026-11-06 10:00:02.010 INFO  worker-5 request id=1ce1ce01 path=/api/jobs status=200 ms=211\n'
            '2026-11-06 10:00:02.512 ERROR worker-1 request id=deadbeef path=/api/tasks status=500 ms=877\n'
            '2026-11-06 10:00:03.100 INFO  worker-1 retry id=deadbeef attempt=2\n'
            '2026-11-06 10:00:03.902 INFO  worker-1 request id=deadbeef path=/api/tasks status=200 ms=402\n'
            '2026-11-06 10:00:04.000 INFO  scheduler heartbeat ok\n'
            '2026-11-06 10:00:04.551 DEBUG worker-7 cache hit key=runs:0099\n')


def outputs():
    tests = ['============================= test session starts ==============================',
             'platform linux -- Python 3.12.7, pytest-8.3.4, pluggy-1.5.0', 'rootdir: /workspace/app',
             'collected 48 items', '']
    for mod in ['test_api.py', 'test_auth.py', 'test_cache.py', 'test_jobs.py', 'test_models.py', 'test_utils.py']:
        tests.append(f'tests/{mod} ' + ''.join(RNG.choice('......F.') for _ in range(8)) + f' [{RNG.randint(10, 99):3d}%]')
    tests += ['', '=================================== FAILURES ===================================']
    for k in range(3):
        tests += [f'___________________________ test_case_{k:02d} ___________________________', '',
                  f'    def test_case_{k:02d}():', f'>       assert compute({k}) == {k * 2 + 1}',
                  f'E       assert {k * 2} == {k * 2 + 1}', '', f'tests/test_api.py:{40 + k * 9}: AssertionError']
    tests += ['=========================== short test summary info ============================']
    tests += [f'FAILED tests/test_api.py::test_case_{k:02d} - assert {k * 2} == {k * 2 + 1}' for k in range(3)]
    tests += ['========================= 3 failed, 45 passed in 4.21s =========================']
    csv = ['report: weekly task outcomes', 'generated: 2026-11-06T09:00:00Z', '',
           'task,runs,pass,fail,timeout,pass_rate']
    for t in range(1, 41):
        runs = RNG.randint(6, 30)
        p = RNG.randint(0, runs)
        f = RNG.randint(0, runs - p)
        csv.append(f'task-{t:03d},{runs},{p},{f},{runs - p - f},{p / runs:.3f}')
    csv += ['', '--- BEGIN TOP 10 ---'] + [f'{i}. task-{RNG.randint(1, 40):03d} {RNG.uniform(0.8, 1):.3f}' for i in range(1, 11)]
    csv += ['--- END TOP 10 ---', '', 'done in 0.84s']
    js = ['{', '  "service": "jobs-api",', '  "window": "2026-11-05T00:00:00Z/2026-11-06T00:00:00Z",',
          '  "endpoints": [']
    eps = ['/api/jobs', '/api/runs', '/api/tasks', '/api/logs', '/api/users', '/api/health']
    for i, ep in enumerate(eps):
        js += ['    {', f'      "path": "{ep}",', f'      "requests": {RNG.randint(1000, 90000)},',
               f'      "p50_ms": {RNG.randint(3, 60)},', f'      "p99_ms": {RNG.randint(100, 2500)},',
               f'      "errors": {RNG.randint(0, 400)}', '    }' + (',' if i < len(eps) - 1 else '')]
    js += ['  ],', '  "alerts": [', '    "p99 above 2s on /api/logs",', '    "error budget 63% spent"', '  ]', '}']
    return [('pytest -q tests', 'test-run', '\n'.join(tests) + '\n',
             '=========================== short test summary info ============================',
             '========================= 3 failed, 45 passed in 4.21s ========================='),
            ('python3 tools/weekly_report.py', 'weekly-report', '\n'.join(csv) + '\n',
             '--- BEGIN TOP 10 ---', '--- END TOP 10 ---'),
            ('python3 tools/latency_summary.py --json', 'latency', '\n'.join(js) + '\n',
             '  "endpoints": [', '  ],')]


def write(name, text):
    with open(os.path.join(FILES, name), 'w', encoding='utf-8', newline='') as f:
        f.write(text)


def main():
    rows = []
    # ---- E2a: append vs full rewrite ----
    sizes = {'S': (5, 10, 10), 'M': (25, 50, 50), 'L': (100, 200, 200)}
    for kind, path in (('md', 'logs/run_log.md'), ('csv', 'logs/metrics.csv'), ('svc', 'logs/service.log')):
        for size, (entries, rows_n, lines_n) in sizes.items():
            if kind == 'md':
                orig, add = md_log(entries), md_entry(entries + 1)
                what = f'the following new run entry (it starts with `## Run {entries + 1:04d}`)'
            elif kind == 'csv':
                orig, add = csv_log(rows_n), csv_rows()
                what = 'the following 6 new CSV rows'
            else:
                orig, add = svc_log(lines_n), svc_lines()
                what = 'the following 8 new log lines'
            tid = f'Wa-{kind}-{size}'
            write(f'{tid}.orig', orig)
            write(f'{tid}.expected', orig + add)
            rows.append({'id': tid, 'exp': 'E2a', 'size': size, 'path': path, 'source_lines': orig.count('\n'),
                         'instruction': (f'Add {what} to the end of `{path}`, after the existing last line. '
                                         'Keep every existing byte unchanged; the file must end with a newline.\n'
                                         f'```\n{add}```'),
                         'source_sha256': sha(orig), 'expected_sha256': sha(orig + add)})
    # ---- E2b: content_ref vs inline content ----
    for cmd, stem, out, begin, end in outputs():
        tool_content = f'$ {cmd}\n{out}\n[exit code: 0]'
        lines = out[:-1].split('\n')
        b, e = lines.index(begin), lines.index(end)
        section = '\n'.join(lines[b + 1:e]) + '\n'
        for variant, expected, desc in (
                ('whole', out, 'the complete program output: every line after the `$ ...` command line, up to '
                               'and including the last output line before the blank line that precedes '
                               '`[exit code: 0]`'),
                ('section', section, f'only the lines strictly between the line `{begin.strip()}` and the line '
                                     f'`{end.strip()}` (both marker lines excluded)')):
            tid = f'Wb-{stem}-{variant}'
            path = f'reports/{stem}{"" if variant == "whole" else "-section"}.txt'
            write(f'{tid}.orig', '')
            write(f'{tid}.expected', expected)
            rows.append({'id': tid, 'exp': 'E2b', 'size': variant, 'path': path, 'command': cmd,
                         'tool_content': tool_content, 'tool_round': 1,
                         'instruction': (f'Create `{path}` containing {desc}, byte-for-byte, ending with a newline. '
                                         'The command ran in tool round 1 (its result is above).'),
                         'source_sha256': sha(''), 'expected_sha256': sha(expected)})
    # ---- E2c: large-file mid-edit ----
    picks = [('requests', 'src/requests/sessions.py', '# NOTE: keep this in sync with the adapter defaults.'),
             ('click', 'src/click/utils.py', '# NOTE: keep this in sync with the stream helpers.'),
             ('click', 'src/click/termui.py', '# NOTE: keep this in sync with the pager helpers.'),
             ('express', 'lib/application.js', '// NOTE: keep this in sync with the router defaults.'),
             ('express', 'lib/response.js', '// NOTE: keep this in sync with the send helpers.'),
             ('ky', 'source/types/options.ts', '// NOTE: keep this in sync with the runtime defaults.')]
    for repo, path, comment in picks:
        commit = subprocess.run(['git', '-C', os.path.join(ROOT, 'repos', repo), 'rev-parse', 'HEAD'],
                                text=True, capture_output=True, check=True).stdout.strip()
        with open(os.path.join(ROOT, 'repos', repo, path), encoding='utf-8', newline='') as f:
            orig = f.read()
        lines = orig[:-1].split('\n')
        mid = len(lines) // 2
        n = next(i for i in range(mid, len(lines)) if lines[i].strip() and lines.count(lines[i]) == 1
                 and lines[i].rstrip().endswith((';', ')', ',')) and not lines[i].lstrip().startswith(('*', '#', '//')))
        indent = lines[n][:len(lines[n]) - len(lines[n].lstrip())]
        new = indent + comment
        expected = '\n'.join(lines[:n + 1] + [new] + lines[n + 1:]) + '\n'
        tid = f'Wc-{repo}-{os.path.basename(path).split(".")[0]}'
        write(f'{tid}.orig', orig)
        write(f'{tid}.expected', expected)
        rows.append({'id': tid, 'exp': 'E2c', 'size': 'L', 'path': path, 'repo': repo, 'commit': commit,
                     'source_lines': len(lines),
                     'instruction': (f'Edit `{path}`. Insert the following line immediately after line {n + 1} '
                                     f'(`{lines[n].strip()}`), as a new line, with exactly this indentation:\n'
                                     f'```\n{new}\n```\nKeep everything else in the file byte-for-byte unchanged.'),
                     'source_sha256': sha(orig), 'expected_sha256': sha(expected)})
    with open(os.path.join(ROOT, 'tasks', 'write_tasks.jsonl'), 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    for row in rows:
        print(row['id'], row['exp'], row['size'], row.get('source_lines'), row['path'])


if __name__ == '__main__':
    main()
