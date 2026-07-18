#!/usr/bin/env python3
"""Headless tests for Review-Mode dash de-slop (2026-07-11).

A peer review must NOT use the em-dash / en-dash as an LLM-slop sentence
separator (the "— " tell). The backend rewrites those deterministically on the
final review body (backend = source of truth), the same way ``smarten_quotes``
educates quotes. Hard rules preserved:
  • a NUMERIC en-dash RANGE (``1–10``, ``2–4``) is legitimate typography — kept.
  • a HYPHEN (``well-motivated``, ``state-of-the-art``) is not a dash — kept.
  • markdown structure (``---`` rule, ``- `` bullet — all ASCII hyphen-minus)
    is untouched.
  • a straight quote / dash that is SYNTAX not punctuation — KaTeX math
    (``$a—b$``), inline/fenced code, URLs — is preserved verbatim.

Coverage:
  • em-dash / horizontal-bar as a prose separator → comma (Latin ", " / CJK "，").
  • CJK double em-dash ``——`` → single fullwidth comma.
  • numeric en-dash range preserved; hyphenated word preserved; ``---`` preserved.
  • math ($...$), code (`...` / ```...```), URLs preserved; surrounding prose deslopped.
  • idempotent + no-op on dash-free / empty text.
  • engine wiring: a review body has its slop dashes removed, a PLAIN report does not.
  • SOURCE-LEVEL NEGATIVE CONTROL: neutering strip_slop_dashes makes the
    engine-wiring assertion fail; restored → passes.

Run standalone: ``python3 tests/test_paper_review_dashes.py``
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

EMDASH, ENDASH, HBAR = '\u2014', '\u2013', '\u2015'
FW_COMMA = '\uff0c'


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ─── Pure de-slopper ─────────────────────────────────────────────

def test_prose_dashes_become_commas():
    from lib.paper import strip_slop_dashes
    assert strip_slop_dashes('The method is novel ' + EMDASH + ' it improves X.') \
        == 'The method is novel, it improves X.'
    # No surrounding spaces.
    assert strip_slop_dashes('novel' + EMDASH + 'it improves') == 'novel, it improves'
    # Horizontal bar (U+2015) behaves like an em-dash.
    assert strip_slop_dashes('a ' + HBAR + ' b') == 'a, b'
    _ok('prose em-dash / horizontal-bar separators become commas')


def test_cjk_dashes_become_fullwidth_comma():
    from lib.paper import strip_slop_dashes
    # Chinese double em-dash slop.
    assert strip_slop_dashes('方法新颖' + EMDASH + EMDASH + '它改进了X。') \
        == '方法新颖' + FW_COMMA + '它改进了X。'
    # Single em-dash after a fullwidth paren.
    assert strip_slop_dashes('4（优秀）' + EMDASH + '主张') == '4（优秀）' + FW_COMMA + '主张'
    _ok('CJK-context dashes become a single fullwidth comma (no ASCII comma in Chinese)')


def test_ranges_hyphens_and_markdown_preserved():
    from lib.paper import strip_slop_dashes
    # Numeric en-dash range — legitimate typography, kept.
    assert strip_slop_dashes('Overall Rating: 1' + ENDASH + '10 range') \
        == 'Overall Rating: 1' + ENDASH + '10 range'
    assert strip_slop_dashes('typically 2' + ENDASH + '4 weaknesses') \
        == 'typically 2' + ENDASH + '4 weaknesses'
    # Hyphen (ASCII) in compound words — not a dash, untouched.
    assert strip_slop_dashes('a well-motivated, state-of-the-art idea') \
        == 'a well-motivated, state-of-the-art idea'
    # Markdown horizontal rule + bullets are ASCII hyphen-minus — untouched.
    assert strip_slop_dashes('---') == '---'
    assert strip_slop_dashes('list:\n- one\n- two') == 'list:\n- one\n- two'
    _ok('numeric en-dash ranges, hyphenated words, and markdown --- / bullets preserved')


def test_math_code_urls_preserved():
    from lib.paper import strip_slop_dashes
    # Em-dash inside code / math is syntax-adjacent — preserved; prose around it deslopped.
    assert strip_slop_dashes('Code `a' + EMDASH + 'b` here' + EMDASH + 'there.') \
        == 'Code `a' + EMDASH + 'b` here, there.'
    assert strip_slop_dashes('Math $a' + EMDASH + 'b$ then' + EMDASH + 'end.') \
        == 'Math $a' + EMDASH + 'b$ then, end.'
    # URL target preserved, surrounding prose deslopped.
    assert strip_slop_dashes('[x](https://a.com/a' + EMDASH + 'b) then' + EMDASH + 'z') \
        == '[x](https://a.com/a' + EMDASH + 'b) then, z'
    _ok('math / code / URL spans preserved; surrounding prose deslopped')


def test_idempotent_and_noop():
    from lib.paper import strip_slop_dashes
    once = strip_slop_dashes('novel ' + EMDASH + ' useful ' + EMDASH + ' fast.')
    assert strip_slop_dashes(once) == once, 'not idempotent'
    assert strip_slop_dashes('no dashes here') == 'no dashes here'
    assert strip_slop_dashes('') == ''
    _ok('de-slopper is idempotent and a no-op on dash-free / empty text')


# ─── Engine wiring: review deslops, plain report does not ────────

def _patch_dispatch(body):
    import lib.paper.report_engine as re_mod

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        if on_content:
            on_content(body)
        return ({'role': 'assistant', 'content': body, 'tool_calls': []},
                'stop', {'_dispatch': {}})

    orig = re_mod.dispatch_stream
    re_mod.dispatch_stream = _fake
    return re_mod, orig


REVIEW_BODY = ('# Review\n\n## Summary\nThe method is novel ' + EMDASH + ' it improves X.\n'
               'Scores use 1' + ENDASH + '10 ranges. Math stays: $a' + EMDASH + 'b$.\n')


def _run(lang_key, ui_lang, phash):
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task
    re_mod2, orig = _patch_dispatch(REVIEW_BODY)
    try:
        task = _new_report_task('t_' + phash[:6], phash, lang_key, None,
                                client_title='P', ui_lang=ui_lang)
        re_mod2._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        return task
    finally:
        re_mod2.dispatch_stream = orig


def test_engine_review_deslops_dashes():
    from lib.paper import make_review_lang
    task = _run(make_review_lang('neurips', 'en'), 'en',
               'phashds0000000000000000000000rev1')
    assert task['status'] == 'done', task.get('error')
    body = task['enriched_text']
    assert 'novel, it improves' in body, 'prose em-dash not converted to comma'
    assert ('novel ' + EMDASH + ' it') not in body, 'a prose em-dash survived in the review'
    assert ('1' + ENDASH + '10') in body, 'numeric en-dash range was destroyed'
    assert ('$a' + EMDASH + 'b$') in body, 'math em-dash was corrupted'
    _ok('engine: a review body has slop dashes removed (ranges + math preserved)')


def test_engine_plain_report_keeps_dashes():
    task = _run('en', 'en', 'phashds0000000000000000000000rep1')
    assert task['status'] == 'done', task.get('error')
    body = task['enriched_text']
    assert ('novel ' + EMDASH + ' it') in body, \
        'plain report should keep em-dashes (de-slop is review-only)'
    _ok('engine: a plain report is NOT deslopped (dash removal is review-only)')


# ─── SOURCE-LEVEL NEGATIVE CONTROL ──────────────────────────────

def test_negative_control_deslopper_is_loadbearing():
    """Neuter strip_slop_dashes to a pass-through → the engine review body keeps
    the em-dash and the positive assertion FAILS. Restore → passes."""
    import lib.paper.review as rv
    from lib.paper import make_review_lang
    saved = rv.strip_slop_dashes

    rv.strip_slop_dashes = lambda text: text or ''  # pass-through
    try:
        task = _run(make_review_lang('neurips', 'en'), 'en',
                   'phashds0000000000000000000000nc01')
        broken = task['enriched_text']
        assert ('novel ' + EMDASH + ' it') in broken, \
            'with the de-slopper neutered, the review must keep the em-dash'
    finally:
        rv.strip_slop_dashes = saved

    task2 = _run(make_review_lang('neurips', 'en'), 'en',
                'phashds0000000000000000000000nc02')
    assert 'novel, it improves' in task2['enriched_text'] \
        and ('novel ' + EMDASH + ' it') not in task2['enriched_text'], \
        'restored de-slopper must remove the review em-dash again'
    _ok('source-level NC: strip_slop_dashes is load-bearing (neuter→dash, restore→comma)')


def main():
    print()
    print(_color('═══ Review-Mode Dash De-slop Tests ═══', '36'))
    print()
    tests = [
        test_prose_dashes_become_commas,
        test_cjk_dashes_become_fullwidth_comma,
        test_ranges_hyphens_and_markdown_preserved,
        test_math_code_urls_preserved,
        test_idempotent_and_noop,
        test_engine_review_deslops_dashes,
        test_engine_plain_report_keeps_dashes,
        test_negative_control_deslopper_is_loadbearing,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
