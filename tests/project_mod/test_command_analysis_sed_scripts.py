"""Sed target discovery reads a script as GNU sed does; script text is never a path.

Literal write targets feed file history and isolated-workspace refusals.
Measured 2026-10-24: target discovery listed every word of an unproven sed
invocation, its script included, as a candidate path, and ``find -exec`` did
the same for nested sed and perl program text.

A ';'-joined script of individually proven commands only edits its in-place
operands. Fields split exactly as GNU sed's ``match_slash`` splits them: a
bracket expression keeps the delimiter and ';' as data, which a naive split
misreads. An unproven invocation names its in-place operands and the files
its w commands name. A program the shell computes is not the literal text
the proof reads.
"""
import shlex
import subprocess

import pytest

from lib.project_mod.command_analysis import (
    command_segment_literal_targets, literal_sed_effects,
)

pytestmark = pytest.mark.unit

# Live traffic, 2026-10-25: refused from cwd /tmp before the fix.
_LIVE_URL_SCRUB = (r's#/conversations/[a-z0-9]+#/conversations/<c>#; '
                   r's#/turns/[0-9a-f-]+#/turns/<t>#; s#\?[^ ]*##')


@pytest.mark.parametrize('program', [
    's/a/b/g; s/c/d/',
    's/a/b/;s/c/d/',
    # Live traffic, 2026-10-24.
    r's|^(lib/[^/]+/[^/]+).*|\1|; s|^(tests/[^/]+).*|\1|',
    _LIVE_URL_SCRUB,
    's#a#x# ;  s#/b#/y#',          # '/' is data under another delimiter
    r's#a\#b#x#',                   # an escaped delimiter is pattern data
    's/a/b/ g ;  s/c/d/2;',
    's;old;new;g; s#a#b#',
    's/[;]/,/g; s/a/b/',           # a bracketed ';' is regex data
    's/[/]/x/; p',                  # GNU sed reads [/] as one bracket expression
    's/[[:space:]]*$//; /^$/d',
    # Addresses that are data: line numbers, $, /regex/, ranges, negation.
    '1d; s/x/y/', '$!d', '/^#/d; s/a/b/I', '1,/^$/s/a/b/p', '/start/,/end/ !d',
    r'/a\/b/s/x/y/', '/x/I,$p; 3q', '5p;10q 2',
    's/é/e/; s/ü/u/',
])
def test_compound_scripts_of_proven_commands_are_filters(program):
    assert literal_sed_effects(['-E', program, 'app.log']) == (False, ('app.log',))
    assert literal_sed_effects(['-r', program, 'app.log']) == (False, ('app.log',))
    assert literal_sed_effects(['-n', '-e', program, '--', 'app.log']) == (False, ('app.log',))
    assert literal_sed_effects(['-i', program, 'app.log']) == (True, ('app.log',))


@pytest.mark.parametrize('program', [
    's/a/b/w out', 's/a/b/;w out', 's/a/b/ ; W out', '1w out',   # write a file
    's/a/b/e', 's/a/b/;e date', '1r /etc/hosts', 'R /etc/hosts',  # execute, read a file
    's#a#b#w out', r's#a\#b#c#w out', 's|a|b|e', 's#a#b# ; 1r in',  # other delimiters
    's/a/b/W out', 's/a/b/r in',                                  # not s flags at all
    's/a/b/\ns/c/d/', 'p\n',                                      # newline-separated
    '{s/a/b/}', '/x/{p;d}', ':a;s/a/b/;ta', 's/a/b/;b', 's/a/b/;T end',  # blocks, labels, branches
    # A naive split reads harmless commands; GNU sed reads the bracket as
    # data and then a w command or flag.
    '/[/p;s/]/w out/g', 's/[/]/;s/w out/g/2',
])
def test_unproven_constructs_stay_unresolved(program):
    assert literal_sed_effects([program, 'app.log']) is None
    assert literal_sed_effects(['-n', '-e', 's/x/y/', '-e', program, 'app.log']) is None


@pytest.mark.parametrize('program,written', [
    ('/[/p;s/]/w out/g', 'out/g'),
    ('s/[/]/;s/w out/g/2', 'out/g/2'),
])
def test_bracket_expressions_split_fields_as_gnu_sed_does(tmp_path, program, written):
    (tmp_path / written).parent.mkdir(parents=True)
    (tmp_path / 'input').write_text('x\n')
    subprocess.run(['sed', '-n', program, 'input'], cwd=tmp_path, check=True,
                   capture_output=True, timeout=30)
    assert (tmp_path / written).is_file()
    assert literal_sed_effects(['-n', program, 'input']) is None
    assert command_segment_literal_targets(
        shlex.join(['sed', '-n', program, 'input'])) == ('input', written)


@pytest.mark.parametrize('command,targets', [
    # Every file an unproven invocation names stays a candidate, as for any
    # unproven utility; its program text never does.
    ("sed -E 's/a/b/;s/c/d/w out.txt' app.log", ('app.log', 'out.txt')),
    ("sed -r 's#a#b#w out' app.log", ('app.log', 'out')),
    ("sed -E 's#a\\#b#c# ; s#x#y#w /tmp/o' /srv/in.log", ('/srv/in.log', '/tmp/o')),
    ("sed -n '/^tmp/,/end/{p}' app.log", ('app.log',)),
    ("sed ':a;s/a/b/;ta' app.log", ('app.log',)),
    ("sed -i 's/a/b/;w copy' app.log", ('app.log', 'copy')),
    ("sed -i -f prog.sed app.log", ('app.log', 'prog.sed')),
    ("sed -n -e 's/a/b/' -e '1w /tmp/log copy' app.log", ('/tmp/log copy', 'app.log')),
    ("sed -es/a/b/wout app.log", ('app.log', 'out')),
    ("sed 's/a/b/w /dev/stdout' app.log", ('app.log',)),
    # A proven filter only reads its operands.
    ("sed -n '/^tmp/,/end/p; s/a/b/' app.log", ()),
    # An unknown option has an unknown arity, so no word has a known role.
    ("sed --future 's/a/b/;w x' app.log", ('app.log', 's/a/b/;w x')),
])
def test_fenced_sed_claims_written_files_never_its_script(command, targets):
    assert command_segment_literal_targets(command) == targets


@pytest.mark.parametrize('command,targets', [
    ("find . -name '*.py' -exec perl -pi -e 's/a/b/' {} +", ('.',)),
    ("find . -name '*.log' -exec sed -i 's/a/b/;w copy' {} +", ('.', 'copy')),
    ("find . -name '*.log' -exec sed -n '/x/{p}' {} \\;", ('.',)),
    # awk shares no candidate fallback with its program text.
    ("find . -name '*.log' -exec awk '{print > \"o\"}' {} +", ('.',)),
    ("awk -i inplace '{ sub(/a/, \"b\") } 1' /srv/x.txt", ('/srv/x.txt',)),
    # An opaque program's words stay candidate paths.
    ("find . -name '*.py' -exec mytool -o report {} +", ('.', 'report')),
])
def test_find_exec_never_claims_nested_program_text(command, targets):
    assert command_segment_literal_targets(command) == targets


@pytest.mark.parametrize('command,targets', [
    ("perl -i -pe 's/a/b/' /srv/x.txt", ('/srv/x.txt',)),
    ("perl -pi.bak -e 's/a/b/' a.py b.py", ('a.py', 'b.py')),
    ("perl -ne 'print if /a/' a.py", ()),
    # -i takes the rest of its cluster as the backup extension: the quoted
    # text is a program file and a.py is the edited operand.
    ("perl -pie 's/a/b/' a.py", ('a.py',)),
])
def test_perl_one_liners_claim_edited_operands_never_code(command, targets):
    assert command_segment_literal_targets(command) == targets
