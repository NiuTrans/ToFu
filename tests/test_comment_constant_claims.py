"""Ratchet: a number a comment asserts about a constant must be the real value.

WHY THIS EXISTS
---------------
``_BUDGET_EXEMPT_TOOLS`` waived the result-size cap for ``read_files`` and
justified it in prose: "these tools already have their own internal limits
(MAX_READ_CHARS=100K per file, BATCH_CHAR_BUDGET=200K)".  The real values were
1_000_000 and 52_428_800 — off by 10x and 262x.  The waiver therefore rested on
two limits that do not limit, and **nothing could ever go red**: a comment is
not executable, so it does not even produce a false red.  It fooled the code's
readers for a long time, and it fooled two people in a review before a
measurement caught it.

This guard makes that class machine-checkable: every "CONST = <number>" claim
written in a comment or docstring is resolved against the constant's ACTUAL
value and must agree.

SCOPE — deliberately narrow, and why
------------------------------------
Only claims naming a constant that is **defined exactly once** (by value)
across ``lib/`` are judged.  Measured on the tree at the time of writing: 86
numeric claims exist in comments/docstrings, of which 5 resolve to a real
constant and 81 are env-var examples (``TOFU_AUTO_RESTART=1``) or prose that
names nothing this can bind.  Judging the unresolvable 81 would mean guessing,
so they are reported as skipped, never failed.

A same-module-only scope was tried first and rejected by measurement: it
resolved 1 of 86 claims and — decisively — **would have missed the very bug
that motivated this guard**, because ``MAX_READ_CHARS`` is claimed in
``compaction/_constants.py`` but defined in ``project_mod/config.py``.
Cross-module resolution is the whole point; do not "simplify" it back.

QUOTED HISTORY IS NOT A CLAIM
-----------------------------
A post-mortem that records a past mistake necessarily repeats the wrong number
("an earlier comment said 100K; the real value is 1_000_000").  Failing that
would force deleting the institutional memory of the bug — the opposite of the
intent.  So a claim inside quotes on a line that also marks it as historical
(``was``/``were``/``earlier``/``old``/``claimed``/``false``…) is exempt, and
``tests`` verify BOTH directions so the exemption cannot swallow live claims.

The same applies to TUNING NARRATION — "with ``_MID_TRAIL=12`` the span
sawtoothed 17→20→23→26" in ``lib/llm/cache.py`` records a value that was tried
and rejected, next to the chosen one.  That is a measured conclusion worth
keeping, so experiment-condition and measured-outcome phrasings are exempt too.
The exemption is deliberately GENEROUS: a missed stale number is a nuisance,
whereas a false accusation pressures someone into deleting real evidence.

WHAT THIS FOUND WHEN IT LANDED: NOTHING — AND THAT IS THE POINT
---------------------------------------------------------------
On the tree where it was written this guard reports **zero** lying comments.
It did not uncover a backlog.  Its whole value is forward-looking: the
``read_files`` waiver's two false numbers sat in the tree for a long time
precisely because no mechanism could ever notice them, and this closes that
door for the next one.  Do not read a green run as "the codebase was audited
and found clean" — read it as "no NEW comment has started lying".
"""

import ast
import collections
import io
import re
import subprocess
import tokenize

import pytest

pytestmark = pytest.mark.unit

#: ``NAME = 123`` / ``NAME is 4_000`` / ``NAME == 100K`` inside prose.
#:
#: The leading ``_?`` is load-bearing, not cosmetic: this repo's tuning
#: constants are overwhelmingly module-private (``_DEFAULT_TOOL_RESULT_MAX``,
#: ``_SINGLE_RESULT_HARD_CEILING_CHARS``, ``_BUDGET_EXEMPT_TOOLS``). A first
#: draft anchored on ``[A-Z]`` and therefore skipped that entire class — the
#: NEUTER injected a false ``_DEFAULT_TOOL_RESULT_MAX = 99_000`` claim and the
#: suite stayed GREEN. Exactly the vacuous guard this file exists to prevent,
#: reproduced inside the guard itself; ``test_private_constant_names_are_scanned``
#: now pins it.
_CLAIM = re.compile(
    r'\b(_?[A-Z][A-Z0-9_]{3,})\s*(?:=|==|\bis\b)\s*([0-9][0-9_,]*)\s*([KkMm])?\b')

#: Words that mark a repeated number as HISTORY rather than a live assertion.
_HISTORICAL = re.compile(
    r'\b(was|were|earlier|previously|old|former|used to|claimed|false|'
    r'wrong|before|instead of|no longer|superseded)\b', re.I)

#: A tuning note narrating an EXPERIMENT CONDITION: "with _MID_TRAIL=12 the
#: span sawtoothed 17→20→23→26". The number is a value that was TRIED and
#: rejected, and the paragraph usually states the chosen value right after.
#:
#: Measured sample: ``lib/llm/cache.py`` records why ``_MID_TRAIL`` went 12→4,
#: including the block spans each value produced. Failing that would force
#: deleting a measured conclusion to win a green tick — destroying exactly the
#: institutional memory the charter says to preserve. Verified against the real
#: source text in ``test_tuning_history_narration_is_exempt``.
#: Only ``with``/``under``/``using`` — these read as "holding this value while
#: observing something". ``when``/``at``/``for`` were tried and REMOVED: they
#: are ordinary descriptive prose ("the stone advances when _MID_STEP = 8
#: rounds elapse"), so including them let a NEUTER-injected false claim pass
#: as history. Measured: with ``when`` in the set, injecting
#: ``_MID_STEP = 99`` into cache.py left the suite GREEN.
_EXPERIMENT_CONDITION = re.compile(
    r'\b(?:with|under|using)\s+_?[A-Z][A-Z0-9_]{3,}\s*=\s*[0-9]',
    re.I)

#: Past-tense measurement verbs that mark the surrounding sentence as a report
#: of an observed outcome rather than a statement of current configuration.
_MEASURED_OUTCOME = re.compile(
    r'\b(sawtoothed|spent|collapsed|collapsing|re-written|rewritten|'
    r'overran|regressed|measured|observed|produced|drifted|thrashed)\b', re.I)

#: Claims that are known-good exceptions. MUST only shrink.
#: Empty by construction — every live claim in lib/ currently agrees with its
#: constant. A new entry needs a comment saying why the number cannot be made
#: true instead.
GRANDFATHERED_STALE_CLAIMS: set[tuple[str, str]] = set()


def _repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracked_lib_files():
    """git ls-files, never os.walk — the latter times out on this FUSE mount."""
    out = subprocess.run(
        ['git', 'ls-files', 'lib/'], cwd=_repo_root(),
        capture_output=True, text=True, timeout=120).stdout.split()
    return [f for f in out if f.endswith('.py')]


def _literal(node):
    """Value of a module-level numeric constant, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        try:
            return eval(compile(ast.Expression(node), '<const>', 'eval'), {}, {})
        except Exception:  # noqa: BLE001 — non-constant expression: not our business
            return None
    return None


def _scan():
    """(constant table, list of prose claims) over the tracked lib/ tree."""
    import os
    root = _repo_root()
    table = collections.defaultdict(dict)   # NAME -> {file: value}
    parsed = {}

    for rel in _tracked_lib_files():
        try:
            with open(os.path.join(root, rel), encoding='utf-8') as f:
                src = f.read()
            tree = ast.parse(src)
        except Exception:  # noqa: BLE001 — unparsable/sibling-WIP file: skip
            continue
        parsed[rel] = (src, tree)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            val = _literal(node.value)
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    table[tgt.id][rel] = val

    claims = []
    for rel, (src, tree) in parsed.items():
        texts = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    texts.append((tok.start[0], tok.string))
        except Exception:  # noqa: BLE001
            pass
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    texts.append((getattr(node, 'lineno', 1), doc))
        for lineno, text in texts:
            for line in text.splitlines():
                for m in _CLAIM.finditer(line):
                    claims.append((rel, lineno, m.group(1), m.group(2),
                                   m.group(3) or '', line))
    return table, claims


def _claimed_value(num: str, unit: str) -> float:
    v = float(num.replace('_', '').replace(',', ''))
    if unit.lower() == 'k':
        v *= 1_000
    elif unit.lower() == 'm':
        v *= 1_000_000
    return v


def _is_historical(line: str, num: str) -> bool:
    """True when the number is QUOTED as a past/incorrect/experimental value.

    Deliberately GENEROUS. A false negative here costs a real stale number
    slipping through; a false positive costs someone deleting a measured
    tuning conclusion to win a green tick. The second is worse and
    irreversible, so when in doubt this returns True.
    """
    # "with _MID_TRAIL=12 the span sawtoothed …" — a rejected trial value.
    if _EXPERIMENT_CONDITION.search(line):
        return True
    # "… sawtoothed 17→20→23→26 and spent HALF the rounds PAST 20" — the
    # sentence reports an observed outcome, so its numbers are data, not config.
    if _MEASURED_OUTCOME.search(line):
        return True
    if not _HISTORICAL.search(line):
        return False
    return ('"' in line or "'" in line or '``' in line
            or 'x the claimed' in line or '× the claimed' in line
            or 'claimed' in line.lower())


def _live_mismatches():
    table, claims = _scan()
    bad = []
    for rel, lineno, name, num, unit, line in claims:
        defs = table.get(name)
        if not defs:
            continue                      # env var / prose — nothing to bind
        values = set(defs.values())
        if len(values) > 1:
            continue                      # same name, several values: ambiguous
        if _is_historical(line, num):
            continue                      # a recorded past mistake, not a claim
        actual = float(next(iter(values)))
        claimed = _claimed_value(num, unit)
        if claimed != actual:
            bad.append((rel, lineno, name, claimed, actual, list(defs)[0]))
    return bad


class TestCommentNumbersMatchConstants:
    def test_no_comment_asserts_a_stale_constant_value(self):
        """Every live "CONST = N" written in prose must state the real N."""
        bad = [b for b in _live_mismatches()
               if (b[0], b[2]) not in GRANDFATHERED_STALE_CLAIMS]
        detail = '\n'.join(
            f'  {rel}:{ln}  {name}: comment says {c:,.0f} but the real value '
            f'is {a:,.0f} (defined in {src})'
            for rel, ln, name, c, a, src in bad)
        assert not bad, (
            f'{len(bad)} comment(s) assert a number that is not the '
            f'constant\'s real value:\n{detail}\n\n'
            'A comment is not executable, so a wrong number here never goes '
            'red on its own — it just misleads every future reader (this is '
            'exactly how the read_files budget exemption stayed broken). Fix '
            'the number, or rewrite the sentence qualitatively.'
        )

    def test_grandfather_list_has_no_dead_entries(self):
        """An exemption whose claim is now correct must be removed."""
        live = {(b[0], b[2]) for b in _live_mismatches()}
        dead = GRANDFATHERED_STALE_CLAIMS - live
        assert not dead, (
            f'these exemptions no longer correspond to a real mismatch — '
            f'drop them so the ratchet keeps tightening: {sorted(dead)}'
        )


class TestDetectorActuallyDiscriminates:
    """Controls. A guard reporting zero is indistinguishable from a dead one."""

    def test_positive_control_the_original_bug_shape_is_caught(self):
        """The exact wording that fooled everyone must be flagged."""
        table = {'MAX_READ_CHARS': {'lib/project_mod/config.py': 1_000_000}}
        line = '# limits (MAX_READ_CHARS=100K per file, BATCH_CHAR_BUDGET=200K).'
        m = _CLAIM.search(line)
        assert m and m.group(1) == 'MAX_READ_CHARS'
        claimed = _claimed_value(m.group(2), m.group(3) or '')
        actual = float(next(iter(table['MAX_READ_CHARS'].values())))
        assert claimed != actual, 'the motivating bug must register as a mismatch'
        assert not _is_historical(line, m.group(2)), (
            'a live justification must NOT be treated as quoted history'
        )

    def test_negative_control_a_correct_claim_passes(self):
        line = '# MAX_THING = 4000 — the real ceiling.'
        m = _CLAIM.search(line)
        assert _claimed_value(m.group(2), m.group(3) or '') == 4000.0

    def test_private_constant_names_are_scanned(self):
        """Module-private constants must be judged too.

        Regression pin: the first draft anchored the name on ``[A-Z]``, so a
        false claim about ``_DEFAULT_TOOL_RESULT_MAX`` was never even parsed
        and the whole suite passed while the injected bug sat in the tree.
        Most tuning constants in this repo are underscore-prefixed, so that
        blind spot covered the majority of what the guard is for.
        """
        line = '# NOTE: _DEFAULT_TOOL_RESULT_MAX = 99_000 per tool.'
        m = _CLAIM.search(line)
        assert m is not None, 'private constant claim was not parsed at all'
        assert m.group(1) == '_DEFAULT_TOOL_RESULT_MAX'
        assert _claimed_value(m.group(2), m.group(3) or '') == 99_000.0

    def test_tuning_history_narration_is_exempt(self):
        """A real tuning note in lib/llm/cache.py must NOT be called a lie.

        ``_MID_TRAIL`` went 12 -> 4 and the comment records what each value
        measured. The paragraph therefore contains ``_MID_TRAIL=12`` while the
        constant is 4 — a mismatch by arithmetic, a valuable record in fact.

        The sample is READ FROM SOURCE, never pasted here: a copy would decouple
        the moment it is written and keep asserting a sentence that may no
        longer exist (charter: never hand-copy production text into a harness).
        """
        import os
        path = os.path.join(_repo_root(), 'lib', 'llm', 'cache.py')
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()

        narration = [ln for ln in lines
                     if '_MID_TRAIL=12' in ln or '_MID_TRAIL = 12' in ln]
        assert narration, (
            'the _MID_TRAIL tuning note is gone from lib/llm/cache.py — if it '
            'was deliberately removed, drop this control; if it was deleted to '
            'silence this guard, that is the failure mode the guard exists to '
            'prevent'
        )
        for line in narration:
            assert _is_historical(line, '12'), (
                f'a tuning-history line would be reported as a stale claim, '
                f'forcing its deletion to go green:\n  {line.strip()}'
            )

    def test_a_live_claim_is_not_excused_by_the_history_rule(self):
        """The exemption must not swallow genuine assertions.

        Without this, widening ``_is_historical`` could quietly turn the whole
        ratchet into a no-op — the exemption is the obvious place for this
        guard to die silently.
        """
        live = '# limits (MAX_READ_CHARS=100K per file, BATCH_CHAR_BUDGET=200K).'
        assert not _is_historical(live, '100')
        live2 = '# NOTE: _DEFAULT_TOOL_RESULT_MAX = 99_000 per tool.'
        assert not _is_historical(live2, '99_000')

    def test_everyday_prepositions_do_not_grant_the_history_pass(self):
        """"when/at/for CONST = N" is plain description, not an experiment log.

        Regression pin: an early draft accepted those prepositions, so the
        NEUTER line "the stone advances when _MID_STEP = 99 rounds elapse"
        was excused as history and the injected falsehood stayed GREEN. Only
        ``with``/``under``/``using`` read as "holding this value while
        observing", which is what a tuning record actually looks like.
        """
        for prep in ('when', 'at', 'for'):
            line = f'# The stone advances {prep} _MID_STEP = 99 rounds elapse.'
            assert not _is_historical(line, '99'), (
                f'"{prep} CONST = N" must NOT be excused as tuning history — '
                f'it is ordinary prose and would hide a real stale number'
            )

        # …while the genuine narration form still gets its pass.
        real = ('# between jumps the rolling tail keeps pulling away: '
                'with _MID_TRAIL=12 the')
        assert _is_historical(real, '12')

    def test_quoted_history_is_exempt_but_only_when_marked(self):
        """Post-mortems keep their numbers; live claims do not get the pass."""
        historical = ('#   * ``MAX_READ_CHARS`` is 1_000_000 (10x the claimed '
                      '100K), which was false.')
        assert _is_historical(historical, '100')

        live = '# limits (MAX_READ_CHARS=100K per file).'
        assert not _is_historical(live, '100')

    def test_unit_suffixes_normalise(self):
        assert _claimed_value('100', 'K') == 100_000.0
        assert _claimed_value('50', 'M') == 50_000_000.0
        assert _claimed_value('1_000_000', '') == 1_000_000.0

    def test_scan_finds_real_constants_and_real_claims(self):
        """Guards the harness itself: if the scan returns nothing, everything
        above passes vacuously — the 'green guard testing nothing' failure."""
        table, claims = _scan()
        assert len(table) > 100, f'constant table looks empty: {len(table)}'
        assert len(claims) > 20, f'claim scan looks empty: {len(claims)}'
