#!/usr/bin/env python3
"""Headless tests for Review-Mode smart-quote education (2026-07-01).

A peer review must ALWAYS render with typographic (smart/curly) quotes,
regardless of what the model emits. Asking the model in the prompt is not
reliable, so the backend educates the quotes deterministically on the final
review body (backend = source of truth). The one hard rule: a straight quote
that is SYNTAX rather than punctuation — KaTeX math (``$f'(x)$`` primes),
inline/fenced code, and URLs — must be preserved verbatim.

Coverage:
  • prose double/single quotes → curly; apostrophes/possessives → ’.
  • math ($...$ / $$...$$), code (`...` / ```...```), and URLs are NOT touched.
  • the educator is idempotent and a no-op on quote-free text.
  • engine wiring: a review task's persisted/enriched body has smart quotes,
    while a PLAIN report (same engine) is left with straight quotes.
  • SOURCE-LEVEL NEGATIVE CONTROL: neutering smarten_quotes to a pass-through
    makes the engine-wiring assertion fail; restored → passes.

Run standalone: ``python3 tests/test_paper_review_smartquotes.py``
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

LSQUO, RSQUO = '\u2018', '\u2019'
LDQUO, RDQUO = '\u201c', '\u201d'


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ─── Pure educator ───────────────────────────────────────────────

def test_prose_quotes_are_curled():
    from lib.paper import smarten_quotes
    assert smarten_quotes('He said "hi".') == f'He said {LDQUO}hi{RDQUO}.'
    assert smarten_quotes("it's the authors' work") == f'it{RSQUO}s the authors{RSQUO} work'
    assert smarten_quotes("the '90s") == f'the {RSQUO}90s'
    # A leading opening single quote before a word becomes an opening curly.
    assert smarten_quotes("'quoted' word") == f'{LSQUO}quoted{RSQUO} word'
    _ok('prose double/single quotes + apostrophes/possessives are educated to curly')


def test_math_code_urls_preserved():
    from lib.paper import smarten_quotes
    # Math primes / double-primes must survive untouched.
    assert smarten_quotes("Math: $f'(x)$ and $g''(y)$.") == "Math: $f'(x)$ and $g''(y)$."
    assert smarten_quotes('Display $$a="b"$$ end.') == 'Display $$a="b"$$ end.'
    # Inline + fenced code preserved.
    assert smarten_quotes('Code `d["k"]` here.') == 'Code `d["k"]` here.'
    assert smarten_quotes('```\nx = "y"\n```') == '```\nx = "y"\n```'
    # URLs (link target, autolink, bare) preserved.
    assert smarten_quotes('[x](https://a.com/q?s="z")') == '[x](https://a.com/q?s="z")'
    assert smarten_quotes('<https://a.com/x\'y>') == '<https://a.com/x\'y>'
    # But prose AROUND a protected span is still educated.
    out = smarten_quotes('It\'s $f\'(x)$ said "so".')
    assert out == f'It{RSQUO}s $f\'(x)$ said {LDQUO}so{RDQUO}.', out
    _ok('math ($...$ primes), code (`...`/```), and URLs are preserved; surrounding prose is educated')


def test_idempotent_and_noop():
    from lib.paper import smarten_quotes
    once = smarten_quotes('He said "hi" to \'them\'.')
    assert smarten_quotes(once) == once, 'not idempotent'
    assert smarten_quotes('no quotes here') == 'no quotes here'
    assert smarten_quotes('') == ''
    _ok('educator is idempotent and a no-op on quote-free / empty text')


# ─── Engine wiring: review curls, plain report does not ──────────

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


REVIEW_BODY = ('# Review\n\n## Summary\nThe authors\' method is "novel" per §3.\n'
               "Math stays: $f'(x)$.\n")


def _run(lang_key, ui_lang, phash):
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task
    re_mod2, orig = _patch_dispatch(REVIEW_BODY)
    try:
        task = _new_report_task('t_' + phash[:6], phash, lang_key, None,
                                client_title='P', ui_lang=ui_lang,
                                config={
                                    'paperInsightEnabled': False,
                                    'paperCheckpointsEnabled': False,
                                })
        re_mod2._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        return task
    finally:
        re_mod2.dispatch_stream = orig


def test_engine_review_curls_quotes():
    from lib.paper import make_review_lang
    task = _run(make_review_lang('neurips', 'en'), 'en',
               'phashsq0000000000000000000000rev1')
    assert task['status'] == 'done', task.get('error')
    body = task['enriched_text']
    assert LDQUO in body and RDQUO in body, 'review double quotes not curled'
    assert RSQUO in body, 'review apostrophe not curled'
    assert '"novel"' not in body, 'a straight double quote survived in the review'
    # Math prime preserved.
    assert "$f'(x)$" in body, 'math prime was corrupted by the educator'
    _ok('engine: a review body is educated to smart quotes (math preserved)')


def test_engine_plain_report_left_straight():
    task = _run('en', 'en', 'phashsq0000000000000000000000rep1')
    assert task['status'] == 'done', task.get('error')
    body = task['enriched_text']
    # The plain-report path must NOT curl quotes — only review mode does.
    assert '"novel"' in body and "authors'" in body, \
        'plain report should keep straight quotes (educator is review-only)'
    _ok('engine: a plain report is NOT educated (smartening is review-only)')


# ─── SOURCE-LEVEL NEGATIVE CONTROL ──────────────────────────────

def test_negative_control_educator_is_loadbearing():
    """Neuter smarten_quotes to a pass-through → the engine review body keeps
    straight quotes and the positive assertion FAILS. Restore → passes."""
    import lib.paper.review as rv
    from lib.paper import make_review_lang
    saved = rv.smarten_quotes

    rv.smarten_quotes = lambda text: text or ''  # pass-through
    try:
        task = _run(make_review_lang('neurips', 'en'), 'en',
                   'phashsq0000000000000000000000nc01')
        broken = task['enriched_text']
        assert '"novel"' in broken and LDQUO not in broken, \
            'with the educator neutered, the review must keep STRAIGHT quotes'
    finally:
        rv.smarten_quotes = saved

    # Restore proven: re-run and the quotes are curled again.
    task2 = _run(make_review_lang('neurips', 'en'), 'en',
                'phashsq0000000000000000000000nc02')
    assert LDQUO in task2['enriched_text'] and '"novel"' not in task2['enriched_text'], \
        'restored educator must curl the review quotes again'
    _ok('source-level NC: smarten_quotes is load-bearing (neuter→straight, restore→curly)')


def main():
    print()
    print(_color('═══ Review-Mode Smart-Quote Tests ═══', '36'))
    print()
    tests = [
        test_prose_quotes_are_curled,
        test_math_code_urls_preserved,
        test_idempotent_and_noop,
        test_engine_review_curls_quotes,
        test_engine_plain_report_left_straight,
        test_negative_control_educator_is_loadbearing,
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
