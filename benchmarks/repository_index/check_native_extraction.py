"""Exact installed-Python oracle for the explicit Rust extraction executable.

Owns synthetic source, temporary grammar faults and joined child processes.
No production selector or performance verdict is produced by this check.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lib.repo_map.outline import Tag, _collect_tree_sitter_tags

FORMAT = 'tofu.repository-extraction/v1'


def corpus():
    """Seven cached grammars, generated identifiers and distinct syntax shapes."""
    templates = {
        'javascript': ('js', 'class Box{n} {{ method{n}() {{ function inner{n}() {{}} }} }}\nfunction top{n}() {{}}\n'),
        'typescript': ('ts', 'interface Shape{n} {{ field: number; }}\nclass Box{n} {{ method{n}(): void {{}} }}\nexport function top{n}(): number {{ return {n}; }}\n'),
        'rust': ('rs', 'struct Box{n} {{ value: i32 }}\nimpl Box{n} {{ fn method{n}(&self) {{}} }}\nfn top{n}() {{}}\n'),
        'go': ('go', 'package sample\ntype Box{n} struct {{ value int }}\nfunc (b Box{n}) Method{n}() {{}}\nfunc Top{n}() {{}}\n'),
        'java': ('java', 'class Box{n} {{ int value; void method{n}() {{ class Inner{n} {{ void inner{n}() {{}} }} }} }}\n'),
        'cpp': ('cpp', 'class Box{n} {{ public: void method{n}() {{}} }};\nint top{n}() {{ return {n}; }}\n'),
        'ruby': ('rb', 'class Box{n}\n def method{n}\n  1\n end\nend\ndef top{n}\nend\n'),
    }
    rows = []
    rng = random.Random(18327)
    for language, (suffix, template) in templates.items():
        for index in range(40):
            source = template.format(n=index)
            if index % 5 == 1:
                source = source.replace('\n', '\r\n')
            elif index % 5 == 2:
                source = source.replace('\n', ' ')
            elif index % 5 == 3:
                source = source[:rng.randrange(1, len(source))]
            elif index % 5 == 4:
                source = '\n\t  ' + source
            rows.append(dict(rel_path=f'{language}/sample_{index}.{suffix}', language=language, source=source))
    for character in ('\n', '\r', '\r\n', '\v', '\f', '\x1c', '\x1d', '\x1e',
                      '\x1f', '\x85', '\u2028', '\u2029', '\u00a0', '\u2003', '\u200b'):
        for source in (f'// preceding{character}function symbol() {{}}\n',
                       f'{character}function symbol() {{ /* {"界😀" * 100} */ }}{character}',
                       f'function first() {{}}{character}function second() {{}}'):
            rows.append(dict(rel_path='unicode.js', language='javascript', source=source))
    rows += [
        dict(rel_path='deep.js', language='javascript', source='function outer() {\n' + '{\n' * 1200 +
             'function inner() {}\n' + '}\n' * 1200 + '}\n'),
        dict(rel_path='names.js', language='javascript', source='function café() {}\nfunction ascii_name() {}\n'
             'class A { ["quoted"]() {} get value() { return 1; } }\nconst arrow = () => 1;'),
        dict(rel_path='empty.js', language='javascript', source=''),
        dict(rel_path='same-path.js', language='javascript', source='function before() {}'),
        dict(rel_path='same-path.js', language='javascript', source='function after() {}'),
        dict(rel_path='additional.cpp', language='cpp', source='class Additional {};'),
    ]
    return rows


def encoded(files):
    return json.dumps(dict(format=FORMAT, files=files), ensure_ascii=False).encode('utf8')


def invoke(binary, grammar_directory, files, *, raw=None, request_limit=None, response_limit=None):
    payload = encoded(files) if raw is None else raw
    result = subprocess.run([str(binary), str(grammar_directory), str(len(payload) if request_limit is None else request_limit),
                             str(max(1_048_576, len(payload) * 64) if response_limit is None else response_limit)],
                            input=payload, capture_output=True, timeout=60)
    return result


def require_response(run):
    assert run.returncode == 0, run.stderr.decode('utf8', 'replace')
    result = json.loads(run.stdout)
    assert set(result) == {'format', 'files'} and result['format'] == FORMAT
    return result['files']


def check(binary, grammar_directory):
    import tree_sitter_language_pack as pack
    assert Path(pack.cache_dir()).resolve() == grammar_directory.resolve(), 'oracle must use the exact supplied grammar directory'
    inputs = corpus()
    for language in {row['language'] for row in inputs}:
        assert (grammar_directory / f'libtree_sitter_{language}.so').is_file(), 'oracle refuses implicit grammar downloads'
    expected = []
    for row in inputs:
        tree = pack.get_parser(row['language']).parse(row['source'].encode('utf8'))
        tags = [asdict(tag) for tag in _collect_tree_sitter_tags(tree, row['rel_path'], row['source'])]
        assert all(set(tag) == {field.name for field in fields(Tag)} for tag in tags)
        expected.append(dict(rel_path=row['rel_path'], tags=tags, error=None))
    # One reused-parser batch and reversed order must match independent Python parsers.
    actual = require_response(invoke(binary, grammar_directory, inputs))
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected)):
        assert left == right, (index, inputs[index], left, right)
    assert require_response(invoke(binary, grammar_directory, list(reversed(inputs)))) == list(reversed(expected))

    healthy = dict(rel_path='healthy.js', language='javascript', source='function healthy() {}')
    good = require_response(invoke(binary, grammar_directory, [healthy]))[0]
    missing = dict(rel_path='missing.zz', language='tofu_missing_grammar', source='not parsed')
    invalid = dict(rel_path='invalid.js', language='../javascript', source='not parsed')
    errors = require_response(invoke(binary, grammar_directory, [missing, healthy, invalid, healthy]))
    assert errors[0] == dict(rel_path='missing.zz', tags=[], error='grammar_unavailable')
    assert errors[2] == dict(rel_path='invalid.js', tags=[], error='invalid_language')
    assert errors[1] == errors[3] == good

    with tempfile.TemporaryDirectory(prefix='tofu-native-grammar-fault-') as directory:
        directory = Path(directory)
        shutil.copyfile(grammar_directory / 'libtree_sitter_javascript.so', directory / 'libtree_sitter_javascript.so')
        (directory / 'libtree_sitter_rust.so').write_bytes(b'not a grammar shared library')
        broken = dict(rel_path='broken.rs', language='rust', source='fn broken() {}')
        result = require_response(invoke(binary, directory, [broken, healthy, broken]))
        assert result[0] == result[2] == dict(rel_path='broken.rs', tags=[], error='grammar_unavailable')
        assert result[1] == good
        # A blocking filesystem operation is owned by the calling process boundary.
        if sys.platform == 'linux':
            (directory / 'libtree_sitter_rust.so').unlink()
            os.mkfifo(directory / 'libtree_sitter_rust.so')
            payload = encoded([broken])
            child = subprocess.Popen([str(binary), str(directory), str(len(payload)), '1048576'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                try:
                    child.communicate(payload, timeout=0.25)
                except subprocess.TimeoutExpired:
                    child.kill()
                    output, _ = child.communicate(timeout=10)
                    assert child.returncode is not None and not output
                else:
                    raise AssertionError('blocked grammar open unexpectedly completed')
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()

    for options in (
        dict(raw=b'{'), dict(raw=b'{"format":"wrong","files":[]}'),
        dict(raw=b'{"format":"tofu.repository-extraction/v1","files":[],"extra":1}'),
        dict(raw=b'{"format":"tofu.repository-extraction/v1","files":[{"rel_path":"x","language":"javascript","source":"\\ud800"}]}'),
        dict(request_limit=0), dict(response_limit=0),
    ):
        failed = invoke(binary, grammar_directory, [healthy], **options)
        assert failed.returncode == 2 and not failed.stdout

    def concurrent(index):
        selected = [index % len(inputs), (index * 19) % len(inputs)]
        assert require_response(invoke(binary, grammar_directory, [inputs[i] for i in selected])) == [expected[i] for i in selected]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(concurrent, range(64)))
    return dict(format='tofu.native-extraction-oracle/v1', cases=len(inputs),
                languages=sorted({row['language'] for row in inputs}), concurrent_joined_calls=64,
                exact_tag_fields=True, reversed_batch_parity=True, per_file_errors=True,
                malformed_and_over_budget_output_withheld=True, blocked_child_killed_and_joined=sys.platform == 'linux',
                performance_admission=False, default_selection_admitted=False)


def receipt(path):
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--grammar-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.binary, args.grammar_dir)
    result.update(binary=receipt(args.binary), grammars={p.name:receipt(p) for p in sorted(args.grammar_dir.glob('*.so'))},
                  sources={str(p.relative_to(ROOT)):receipt(p)['sha256'] for p in [
                      Path(__file__), ROOT/'lib/repo_map/outline.py',
                      *sorted((ROOT/'packages/tofu-repo-kernel/rust/extraction').glob('src/*.rs')),
                      ROOT/'packages/tofu-repo-kernel/rust/extraction/Cargo.toml',
                      ROOT/'packages/tofu-repo-kernel/rust/extraction/Cargo.lock']})
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('sources','grammars','binary')}))


if __name__ == '__main__':
    main()
