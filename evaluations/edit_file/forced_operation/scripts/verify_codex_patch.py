"""Pin codex_patch.py to the vendored Codex apply-patch behaviour (codex @ 5c5308fc9a9e).

1. Every scenario fixture in codex-rs/apply-patch/tests/fixtures/scenarios is replayed
   through a CLI-mode driver (sequential hunks, partial changes on failure, as
   lib.rs::apply_hunks_to_files does) and the final directory must equal expected/.
   023/024 exercise CRLF/mixed line endings under PreserveLineEndings; the benchmark
   files are LF-only and the port implements the default NormalizeToLf, so those two
   are reported as skipped, not passed.
2. Parser error texts from streaming_parser.rs / parser.rs unit tests.
3. Core-tool semantics: verification before any write; two hunks on one path refused;
   a context-free pure addition appends at end of file (016 fixture semantics).

Run: EDITBENCH_CODEX_DIR=<codex checkout> python3 verify_codex_patch.py   (exit 1 on any failure)
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import codex_patch as cp  # noqa: E402

CODEX = os.environ.get('EDITBENCH_CODEX_DIR', '')
SCEN = os.path.join(CODEX, 'codex-rs/apply-patch/tests/fixtures/scenarios')
SKIP = {'023_preserves_crlf_line_endings': 'CRLF (PreserveLineEndings) not ported',
        '024_preserves_mixed_line_endings': 'mixed endings (PreserveLineEndings) not ported'}


def cli_apply(patch, root):
    try:
        hunks = cp.parse_patch(patch)
    except cp.ParseError:
        return False
    if not hunks:
        return False
    for hunk in hunks:
        path = os.path.join(root, hunk['path'])
        if hunk['type'] == 'add':
            os.makedirs(os.path.dirname(path) or root, exist_ok=True)
            with open(path, 'w', newline='') as f:
                f.write(hunk['contents'])
        elif hunk['type'] == 'delete':
            if not os.path.isfile(path):
                return False
            os.remove(path)
        else:
            if not os.path.isfile(path):
                return False
            with open(path, newline='') as f:
                original = f.read()
            try:
                new = cp.derive_new_contents(original, hunk['path'], hunk['chunks'])
            except cp.ApplyError:
                return False
            if hunk['move_path']:
                dest = os.path.join(root, hunk['move_path'])
                os.makedirs(os.path.dirname(dest) or root, exist_ok=True)
                with open(dest, 'w', newline='') as f:
                    f.write(new)
                os.remove(path)
            else:
                with open(path, 'w', newline='') as f:
                    f.write(new)
    return True


def snapshot(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for d in dirs:
            rel = os.path.relpath(os.path.join(base, d), root)
            out[rel + '/'] = None
        for name in files:
            full = os.path.join(base, name)
            with open(full, 'rb') as f:
                out[os.path.relpath(full, root)] = f.read()
    return out


def run_scenarios():
    passed, failed, skipped = [], [], []
    for name in sorted(os.listdir(SCEN)):
        d = os.path.join(SCEN, name)
        if not os.path.isdir(d):
            continue
        if name in SKIP:
            skipped.append(name)
            continue
        tmp = tempfile.mkdtemp(prefix='codexscen-')
        try:
            if os.path.isdir(os.path.join(d, 'input')):
                shutil.copytree(os.path.join(d, 'input'), tmp, dirs_exist_ok=True)
            with open(os.path.join(d, 'patch.txt'), newline='') as f:
                patch = f.read()
            cli_apply(patch, tmp)
            exp_dir = os.path.join(d, 'expected')
            expected = snapshot(exp_dir) if os.path.isdir(exp_dir) else {}
            (passed if snapshot(tmp) == expected else failed).append(name)
        finally:
            shutil.rmtree(tmp)
    return passed, failed, skipped


def expect_error(patch, text):
    try:
        cp.parse_patch(patch)
    except cp.ParseError as e:
        assert str(e) == text, (str(e), text)
        return
    raise AssertionError('no error for ' + repr(patch))


def run_unit():
    expect_error('bad', "invalid patch: The first line of the patch must be '*** Begin Patch'")
    expect_error('*** Begin Patch\nbad', "invalid patch: The last line of the patch must be '*** End Patch'")
    expect_error('*** Begin Patch\n*** Update File: test.py\n*** End Patch',
                 "invalid hunk at line 2, Update file hunk for path 'test.py' is empty")
    expect_error('*** Begin Patch\nfoo\n*** End Patch',
                 "invalid hunk at line 2, 'foo' is not a valid hunk header. Valid hunk headers: "
                 "'*** Add File: {path}', '*** Delete File: {path}', '*** Update File: {path}'")
    hunks = cp.parse_patch("<<'EOF'\n*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\nEOF\n")
    assert hunks == [{'type': 'add', 'path': 'a.txt', 'contents': 'hi\n'}], hunks
    files = {'f.py': 'a\nb\nc\n'}
    ok, msg = cp.apply_patch('*** Begin Patch\n*** Update File: f.py\n@@\n b\n+x\n*** End Patch',
                             files, lambda p: p)
    assert ok and files['f.py'] == 'a\nb\nx\nc\n', (ok, msg, files)
    files = {'f.py': 'a\nb\nc\n'}
    ok, msg = cp.apply_patch('*** Begin Patch\n*** Update File: f.py\n@@\n+x\n*** End Patch',
                             files, lambda p: p)
    assert ok and files['f.py'] == 'a\nb\nc\nx\n', ('context-free addition appends at EOF', files)
    files = {'f.py': 'a\nb\nc\n'}
    ok, msg = cp.apply_patch('*** Begin Patch\n*** Update File: f.py\n@@\n-b\n+B\n*** Update File: f.py\n'
                             '@@\n-c\n+C\n*** End Patch', files, lambda p: p)
    assert not ok and 'multiple operations target f.py' in msg and files['f.py'] == 'a\nb\nc\n', msg
    files = {'f.py': 'a\nb\nc\n', 'g.py': 'x\n'}
    ok, msg = cp.apply_patch('*** Begin Patch\n*** Update File: g.py\n@@\n-x\n+y\n*** Update File: f.py\n'
                             '@@\n-zzz\n+C\n*** End Patch', files, lambda p: p)
    assert not ok and files['g.py'] == 'x\n', 'core verifies every hunk before writing'
    assert msg.startswith('apply_patch verification failed: Failed to find expected lines in f.py'), msg
    files = {'f.py': '    a\nb\n'}
    ok, msg = cp.apply_patch('*** Begin Patch\n*** Update File: f.py\n@@\n-a\n+z\n*** End Patch',
                             files, lambda p: p)
    assert ok and files['f.py'] == 'z\nb\n', ('trim-both rung replaces the whole matched line', files)


if __name__ == '__main__':
    run_unit()
    passed, failed, skipped = run_scenarios()
    print(f'unit: ok; scenarios passed={len(passed)} failed={failed} skipped={skipped}')
    sys.exit(1 if failed else 0)
