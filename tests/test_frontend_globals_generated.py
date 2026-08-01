"""Guard: ``static/js/globals.generated.d.ts`` must match the frontend source.

Why a generated ambient file (and why it must be guarded)
---------------------------------------------------------
Tofu's frontend is a concatenated bundle sharing ONE global scope, so a
module's public surface is whatever it leaves on ``window`` — an implicit
contract ``tsc --checkJs`` cannot see. The obvious fix, hand-writing
``declare var X: any;`` per complaint, is the wrong one: a hand-written
declaration DOWNGRADES THE CONTRACT TO A COMMENT. It keeps asserting a symbol
exists after the symbol is renamed or deleted, which silences the exact bug
class (undefined cross-file reference) the type-check was installed to catch.

``scripts/gen_frontend_globals.py`` therefore DERIVES the declarations from the
code. This guard is what makes that derivation binding: if someone renames a
symbol and does not regenerate, ``--check`` fails and CI goes red, instead of
the ambient file quietly describing a codebase that no longer exists.

Measured impact when this landed: tsc app-source errors 92 → 13, with every
remaining error a genuine type defect rather than symbol-resolution noise.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
GEN = os.path.join(ROOT, 'scripts', 'gen_frontend_globals.py')
OUT = os.path.join(ROOT, 'static', 'js', 'globals.generated.d.ts')
TSCONFIG = os.path.join(ROOT, 'tsconfig.json')


@pytest.mark.unit
def test_generator_and_output_exist():
    assert os.path.exists(GEN), 'scripts/gen_frontend_globals.py is missing'
    assert os.path.exists(OUT), (
        'static/js/globals.generated.d.ts is missing — generate it with:\n'
        '    python3 scripts/gen_frontend_globals.py'
    )


@pytest.mark.unit
def test_generated_globals_are_up_to_date():
    """The committed .d.ts must equal a fresh regeneration.

    Fails whenever a frontend symbol is added, renamed or removed without
    regenerating — which is precisely the drift a hand-maintained ambient file
    would have absorbed silently.
    """
    proc = subprocess.run(
        [sys.executable, GEN, '--check'],
        capture_output=True, text=True, timeout=300, cwd=ROOT,
    )
    if proc.returncode != 0:
        pytest.fail(
            'globals.generated.d.ts is STALE relative to static/js.\n'
            f'{proc.stdout}{proc.stderr}\n'
            'Regenerate and commit:\n'
            '    python3 scripts/gen_frontend_globals.py'
        )


@pytest.mark.unit
def test_generated_file_is_in_the_tsc_program():
    """A declaration file tsc never loads protects nothing."""
    with open(TSCONFIG, encoding='utf-8') as fh:
        cfg = fh.read()
    assert 'globals.generated.d.ts' in cfg, (
        'tsconfig.json must include static/js/globals.generated.d.ts, '
        'otherwise the generated declarations are dead weight.'
    )


@pytest.mark.unit
def test_generated_file_is_marked_do_not_edit():
    """A generated file that looks hand-editable WILL be hand-edited."""
    with open(OUT, encoding='utf-8') as fh:
        head = fh.read(600)
    assert 'AUTO-GENERATED' in head and 'DO NOT EDIT' in head, (
        'the generated .d.ts must carry an AUTO-GENERATED / DO NOT EDIT banner '
        'naming the generator, or an edit to it will be silently overwritten.'
    )


@pytest.mark.unit
def test_ambient_dom_widening_stays_scoped_not_blanket():
    """The hand-written DOM widening must list PROPERTIES, never open the door.

    ``globals.d.ts`` legitimately widens a few lib.dom interfaces (Element /
    EventTarget / Navigator / …) because tsc cannot narrow an untyped
    ``getElementById()`` result, and a Chromium-only API like
    ``navigator.userAgentData`` is not in the configured ``lib`` at all. That
    loosening is only defensible while it stays an EXPLICIT property list.

    An index signature — ``[key: string]: any`` — would end the ratchet as a
    bug-finder in one line: every property typo on every widened interface
    would type-check clean, and the file would still look like a careful
    declaration. It is the same failure this module's docstring describes for
    hand-written ``declare var``, one level down: the contract degrades into a
    comment, silently, while the suite stays green.

    So the shape is guarded, not the contents: adding a named property is
    ordinary maintenance and needs no test change; reaching for a blanket
    signature fails here and has to be argued for.
    """
    path = os.path.join(ROOT, 'static', 'js', 'globals.d.ts')
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    # Comments are stripped first: this file EXPLAINS the rule in prose, and a
    # guard a comment can trip is a false alarm that trains people to ignore it
    # (tests/_source_scan.py module docstring, incident family 1-3).
    from tests._source_scan import strip_comments
    live = strip_comments(src, lang='js', inline=True)
    offenders = [
        ln.strip() for ln in live.splitlines()
        if re.search(r'\[\s*\w+\s*:\s*string\s*\]\s*:', ln)
    ]
    assert not offenders, (
        'globals.d.ts declares a blanket index signature:\n  '
        + '\n  '.join(offenders)
        + '\nThat makes EVERY property name valid on the widened interface, so '
          'a typo like `navigator.userAgentDataX` would no longer be reported '
          'and the tsc ratchet would stop catching the bug class it exists '
          'for. Widen with an explicit property list instead.'
    )


@pytest.mark.unit
def test_no_handwritten_declare_var_creep_in_ambient_file():
    """The hand-maintained globals.d.ts stays scoped to third-party libs.

    That file exists to silence vendored globals (katex/marked/hljs/...). App
    symbols belong in the GENERATED file, where they are derived from and stay
    tied to the code. Letting app symbols accumulate here by hand is how the
    contract rots back into a comment.
    """
    with open(os.path.join(ROOT, 'static', 'js', 'globals.d.ts'),
              encoding='utf-8') as fh:
        src = fh.read()
    for banned in ('isChatModel', 'reduceStreamState', 'renderChat',
                   'conversations', 'activeConvId'):
        assert f'declare var {banned}' not in src, (
            f'{banned} is hand-declared in globals.d.ts. App symbols must come '
            'from scripts/gen_frontend_globals.py so a rename cannot leave a '
            'stale declaration behind.'
        )
