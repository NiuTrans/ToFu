"""Pin cc_edit.py to Claude Code 2.1.207 Edit semantics read from its bundle strings.

Each case cites the minified function it exercises. Run: python3 verify_cc_edit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import cc_edit as cc  # noqa: E402


def run(content, **args):
    files = {'f.py': content}
    args.setdefault('file_path', 'f.py')
    ok, msg, kind = cc.cc_edit(files, args, lambda p: p)
    return ok, msg, kind, files['f.py']


def main():
    # validateInput errorCode 1
    ok, msg, kind, _ = run('a\n', old_string='a', new_string='a')
    assert not ok and msg == 'No changes to make: old_string and new_string are exactly the same.'
    # errorCode 8 (Mrt returns null)
    ok, msg, kind, _ = run('a\n', old_string='zz', new_string='y')
    assert not ok and msg == 'String to replace not found in file.\nString: zz', msg
    # errorCode 9
    ok, msg, kind, _ = run('x\nx\n', old_string='x', new_string='y')
    assert not ok and msg.startswith('Found 2 matches of the string to replace, but replace_all is false.')
    assert msg.endswith('\nString: x')
    # replace_all
    ok, msg, kind, out = run('x\nx\n', old_string='x', new_string='y', replace_all=True)
    assert ok and out == 'y\ny\n' and 'All occurrences were successfully replaced.' in msg
    # WTu: empty new_string also removes the following line break
    ok, msg, kind, out = run('a\nb\nc\n', old_string='b', new_string='')
    assert ok and out == 'a\nc\n', out
    ok, msg, kind, out = run('a b c\n', old_string='b ', new_string='')
    assert ok and out == 'a c\n', out
    # Mrt quote normalization (BTu) and qYt style preservation
    ok, msg, kind, out = run('say \u201chi\u201d\n', old_string='say "hi"', new_string='say "yo"')
    assert ok and out == 'say \u201cyo\u201d\n', out
    # Mrt unicode escape decoding (h7i)
    ok, msg, kind, out = run('t = "\u23f0"\n', old_string='t = "\\u23f0"', new_string='t = "\\u23f1"')
    assert ok and out == 't = "\u23f1"\n', repr(out)
    # oTu aliases
    ok, msg, kind, out = run('a\n', old_str='a', new_str='b')
    assert ok and out == 'b\n'
    # strictObject: unknown keys rejected
    ok, msg, kind, _ = run('a\n', old_string='a', new_string='b', operation='replace')
    assert not ok and kind == 'schema', msg
    # success text (mapToolResultToToolResultBlockParam + LUn)
    ok, msg, kind, _ = run('a\n', old_string='a', new_string='b')
    assert msg == ('The file f.py has been updated successfully. (file state is current in your '
                   'context \u2014 no need to Read it back)'), msg
    # exact match is required: no whitespace or indentation fallback
    ok, msg, kind, _ = run('    a = 1\n', old_string='a  = 1', new_string='a = 2')
    assert not ok and kind == 'not_found'
    print('cc_edit: all pinned cases pass')


if __name__ == '__main__':
    main()
