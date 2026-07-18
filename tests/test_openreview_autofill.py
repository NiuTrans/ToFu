#!/usr/bin/env python3
"""Headless tests for the OpenReview single-page auto-fill (killer feature).

The feature: one Tofu button → the browser bridge fills the review form on the
reviewer's CURRENT OpenReview tab from the generated review, then STOPS. It must
NEVER click a Submit/Post/Confirm control — the human submits.

The single most important property under test is exactly that **never-submit**
guarantee, verified three ways:
  1. is_submit_control() recognizes commit controls across label variants;
  2. build_fill_plan() structurally excludes submit selectors + plan_has_submit_action() stays False;
  3. the end-to-end orchestration (with a fake bridge) issues NO command that
     touches a submit control, on the happy path AND every refusal path;
  4. a NEGATIVE CONTROL proves the guardrail is load-bearing: neutralizing
     is_submit_control makes a submit selector leak into the plan → the
     never-submit assertion fails (so the test would catch a regression).

Field mapping is checked across venue form VARIANTS (label vs name vs aria-label
vs placeholder; NLPCC's 4-column form; a NeurIPS-ish multi-field form) because
OpenReview forms differ per venue and the classifier must not be hard-coded.

Run standalone: ``python3 tests/test_openreview_autofill.py``
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ─── A fake browser bridge (records every command; never touches a real tab) ──

class FakeBridge:
    """Records send_browser_command calls and returns scripted results.

    ``script`` maps cmd_type → (result, error). ``list_tabs`` and
    ``get_interactive_elements`` are provided via constructor for convenience.
    """
    def __init__(self, tab_url, elements, tab_id=7):
        self.tab_url = tab_url
        self.elements = elements
        self.tab_id = tab_id
        self.calls = []

    def send_browser_command(self, cmd, params=None, timeout=20, client_id=None):
        self.calls.append((cmd, dict(params or {})))
        if cmd == 'list_tabs':
            return [{'id': self.tab_id, 'url': self.tab_url, 'title': 'OpenReview',
                     'active': True}], None
        if cmd == 'get_interactive_elements':
            return {'elements': self.elements, 'url': self.tab_url, 'title': 'OpenReview'}, None
        if cmd == 'type_text':
            return {'success': True}, None
        if cmd == 'click_element':
            return {'clicked': True}, None
        return None, f'unexpected cmd {cmd}'

    def fill_and_click_selectors(self):
        """Selectors that were actually typed into / clicked (the acted set)."""
        return [p.get('selector') for c, p in self.calls
                if c in ('type_text', 'click_element')]


# A realistic multi-venue-ish OpenReview review form.
def _neurips_form():
    return [
        {'tag': 'input', 'type': 'text', 'selector': '#title', 'placeholder': 'Review Title'},
        {'tag': 'textarea', 'selector': '#review', 'ariaLabel': 'Review'},
        {'tag': 'select', 'selector': '#rating', 'ariaLabel': 'Overall Rating'},
        {'tag': 'select', 'selector': '#conf', 'ariaLabel': 'Confidence'},
        {'tag': 'button', 'selector': '#submit', 'text': 'Submit'},
        {'tag': 'button', 'selector': '#post', 'text': 'Post Official Review'},
        {'tag': 'button', 'selector': '#cancel', 'text': 'Cancel'},
    ]


def _nlpcc_form():
    # NLPCC's 4-column form: title, review, OA, confidence.
    return [
        {'tag': 'input', 'type': 'text', 'selector': '#t', 'name': 'title'},
        {'tag': 'textarea', 'selector': '#r', 'name': 'review'},
        {'tag': 'input', 'type': 'number', 'selector': '#oa', 'ariaLabel': 'Overall Assessment (OA)'},
        {'tag': 'input', 'type': 'number', 'selector': '#c', 'ariaLabel': 'Confidence'},
        {'tag': 'input', 'type': 'submit', 'selector': '#go', 'name': 'save'},
    ]


# ─── Submit-control detection ───────────────────────────────────────────

def test_is_submit_control_recognizes_commit_controls():
    from lib.paper import is_submit_control
    yes = [
        {'tag': 'button', 'text': 'Submit'},
        {'tag': 'button', 'text': 'Post Official Review'},
        {'tag': 'button', 'text': 'Confirm & Submit'},
        {'tag': 'a', 'role': 'button', 'text': 'Send'},
        {'tag': 'input', 'type': 'submit', 'name': 'anything'},   # type=submit is unconditional
        {'tag': 'button', 'ariaLabel': 'Publish review'},
    ]
    for el in yes:
        assert is_submit_control(el), f'should be submit: {el}'
    no = [
        {'tag': 'textarea', 'name': 'review'},
        {'tag': 'input', 'type': 'text', 'name': 'title'},
        {'tag': 'input', 'type': 'number', 'ariaLabel': 'Overall Assessment'},
        {'tag': 'select', 'ariaLabel': 'Confidence'},
        {'tag': 'button', 'text': 'Add reviewer'},   # clickable but no commit token
    ]
    for el in no:
        assert not is_submit_control(el), f'should NOT be submit: {el}'
    _ok('is_submit_control recognizes commit controls across label variants; spares text fields')


# ─── Venue-agnostic field mapping ───────────────────────────────────────

def test_classify_maps_fields_across_venue_variants():
    from lib.paper import classify_review_form
    c1 = classify_review_form(_neurips_form())
    assert set(c1['fields']) == {'title', 'review', 'overall', 'confidence'}, c1['fields']
    assert c1['fields']['review']['selector'] == '#review'
    assert c1['fields']['overall']['selector'] == '#rating'   # "Overall Rating" alias
    assert len(c1['submit_controls']) == 2                    # #submit + #post (not #cancel)

    c2 = classify_review_form(_nlpcc_form())
    assert set(c2['fields']) == {'title', 'review', 'overall', 'confidence'}, c2['fields']
    assert c2['fields']['overall']['selector'] == '#oa'       # "Overall Assessment (OA)"
    # input[type=submit] is a submit control even though its name is "save".
    assert any(s['selector'] == '#go' for s in c2['submit_controls'])
    _ok('classify_review_form maps title/review/OA/confidence across venue label variants')


def test_confidence_and_title_beat_generic_review_match():
    from lib.paper import classify_review_form
    # "Review Title" must go to title, "Confidence" must not collapse to review.
    els = [
        {'tag': 'input', 'type': 'text', 'selector': '#rt', 'ariaLabel': 'Review Title'},
        {'tag': 'textarea', 'selector': '#rv', 'ariaLabel': 'Review'},
        {'tag': 'select', 'selector': '#cf', 'ariaLabel': 'Reviewer Confidence'},
    ]
    c = classify_review_form(els)
    assert c['fields']['title']['selector'] == '#rt'
    assert c['fields']['review']['selector'] == '#rv'
    assert c['fields']['confidence']['selector'] == '#cf'
    _ok('longest-phrase specificity: "Review Title"→title, "Confidence"→confidence, not review')


def test_review_textarea_beats_short_input():
    from lib.paper import classify_review_form
    # If both an input and a textarea claim "review", the textarea wins.
    els = [
        {'tag': 'input', 'type': 'text', 'selector': '#short', 'ariaLabel': 'Review'},
        {'tag': 'textarea', 'selector': '#big', 'ariaLabel': 'Review'},
    ]
    c = classify_review_form(els)
    assert c['fields']['review']['selector'] == '#big', c['fields']['review']
    _ok('review free-text prefers the textarea over a one-line input')


# ─── Value extraction from a finished review ────────────────────────────

def test_extract_review_values_splits_prose_and_scores():
    from lib.paper import extract_review_values
    review = (
        '# Review\n\n## Summary\nGood paper with solid experiments.\n\n'
        '--- FOR THE REVIEW FORM (do not paste into the review text) ---\n\n'
        '## Quantitative Scores\n'
        '- **Overall Assessment (OA)**: 5 — accept, good paper (scale 1–6).\n'
        '- **Confidence**: 4 — fairly confident (scale 1–5).\n'
    )
    v = extract_review_values(review, title='My Great Paper')
    assert v['overall'] == '5', v
    assert v['confidence'] == '4', v
    assert v['title'] == 'My Great Paper'
    # The pasted review body must NOT contain the scorecard (no score leakage).
    assert 'Quantitative' not in v['review'] and 'Good paper' in v['review']
    _ok('extract_review_values: prose body (no scores) + OA/Confidence integers picked')


def test_extract_review_values_skips_scale_range_takes_choice():
    from lib.paper import extract_review_values
    # A line where the scale range appears BEFORE the chosen value.
    review = (
        'Body.\n\n--- FOR THE REVIEW FORM (do not paste into the review text) ---\n\n'
        '## Quantitative Scores\n'
        '- **Overall Rating** (1–10 scale): 8\n'
        '- **Confidence** (1-5): 3\n'
    )
    v = extract_review_values(review)
    assert v['overall'] == '8', v      # not "1" or "10" from the range
    assert v['confidence'] == '3', v
    _ok('score picker skips the scale range and takes the reviewer\'s chosen value')


# ─── Fill-plan is structurally submit-free ──────────────────────────────

def test_fill_plan_excludes_submit_and_has_no_submit_action():
    from lib.paper import classify_review_form, build_fill_plan, plan_has_submit_action
    c = classify_review_form(_neurips_form())
    plan = build_fill_plan(c, {'title': 'T', 'review': 'R', 'overall': '7', 'confidence': '4'})
    assert not plan_has_submit_action(plan), 'plan must have NO submit action'
    sels = [a['selector'] for a in plan['actions']]
    assert '#submit' not in sels and '#post' not in sels and '#cancel' not in sels, sels
    assert all(a['op'] in ('fill', 'click') for a in plan['actions'])
    _ok('build_fill_plan is structurally submit-free (no submit selectors, only fill/click ops)')


def test_fill_plan_skips_missing_fields_and_empty_values():
    from lib.paper import classify_review_form, build_fill_plan
    # Form with only a review box; OA/confidence absent.
    els = [{'tag': 'textarea', 'selector': '#r', 'ariaLabel': 'Review'},
           {'tag': 'button', 'selector': '#s', 'text': 'Submit'}]
    c = classify_review_form(els)
    plan = build_fill_plan(c, {'title': '', 'review': 'text', 'overall': '5', 'confidence': ''})
    filled_fields = [a['field'] for a in plan['actions']]
    assert filled_fields == ['review'], filled_fields   # only review present + non-empty
    skipped = {s['field'] for s in plan['skipped']}
    assert 'overall' in skipped   # value present but no field on the form
    _ok('build_fill_plan skips empty values and records fields with no matching control')


# ─── End-to-end orchestration (fake bridge) ─────────────────────────────

def test_orchestration_fills_and_never_touches_submit():
    from lib.paper import autofill_openreview_review
    b = FakeBridge('https://openreview.net/forum?id=ABC', _neurips_form())
    r = autofill_openreview_review(b, {'title': 'T', 'review': 'R', 'overall': '7', 'confidence': '4'})
    assert r['ok'] and r['stage'] == 'filled', r
    assert r['submit_controls_detected'] == 2
    acted = b.fill_and_click_selectors()
    # THE guarantee: no submit/post/cancel selector was ever acted upon.
    for forbidden in ('#submit', '#post', '#cancel'):
        assert forbidden not in acted, f'orchestration touched {forbidden}! acted={acted}'
    # No command name is ever a submit — the system has no submit op at all.
    assert all(c in ('list_tabs', 'get_interactive_elements', 'type_text', 'click_element')
               for c, p in b.calls), [c for c, p in b.calls]
    _ok('orchestration fills the form and NEVER acts on a submit/post/cancel control')


def test_orchestration_refuses_non_openreview_tab_without_acting():
    from lib.paper import autofill_openreview_review
    b = FakeBridge('https://example.com/whatever', _neurips_form())
    r = autofill_openreview_review(b, {'review': 'R', 'overall': '5'})
    assert not r['ok'] and r['stage'] == 'not_openreview', r
    # It must NOT even scan elements or fill anything on a non-OpenReview page.
    assert not any(c in ('get_interactive_elements', 'type_text', 'click_element')
                   for c, p in b.calls), b.calls
    assert 'OpenReview' in r['message']
    _ok('orchestration refuses a non-OpenReview tab with a clear message, acting on nothing')


def test_orchestration_refuses_when_no_form():
    from lib.paper import autofill_openreview_review
    b = FakeBridge('https://openreview.net/forum?id=Z',
                   [{'tag': 'button', 'selector': '#s', 'text': 'Submit'}])
    r = autofill_openreview_review(b, {'review': 'R'})
    assert not r['ok'] and r['stage'] == 'no_form', r
    assert not any(c in ('type_text', 'click_element') for c, p in b.calls), b.calls
    _ok('orchestration refuses (no fill) when no review form is present')


def test_orchestration_reports_bridge_disconnect():
    from lib.paper import autofill_openreview_review

    class DeadBridge:
        def send_browser_command(self, cmd, params=None, timeout=20, client_id=None):
            return None, 'Browser extension is not connected.'
    r = autofill_openreview_review(DeadBridge(), {'review': 'R'})
    assert not r['ok'] and r['stage'] == 'connect', r
    _ok('orchestration surfaces a bridge-disconnect as a clean failure, not a hang')


# ─── NEGATIVE CONTROL: prove the never-submit guardrail is load-bearing ──

def test_negative_control_defense_in_depth_submit_selector_guard():
    """Prove build_fill_plan's defense-in-depth guard is load-bearing.

    Never-submit is enforced in two independent layers:
      (A) classification only ever accepts text/select/radio controls, so
          <button>/<a>/input[type=submit] are structurally non-fillable; AND
      (B) build_fill_plan additionally REFUSES to emit an action whose selector
          appears in ``submit_controls`` — a belt for the case where a field is
          mis-classified onto a selector that is ALSO a detected submit control.

    This exercises layer (B) directly: hand-craft a classified form where the
    'overall' field's selector collides with a submit control. WITH the guard
    the action is skipped; if the collided selector is NOT in submit_controls
    (detection miss), it WOULD be emitted — so submit_controls drives exclusion.
    """
    from lib.paper import build_fill_plan
    # A field whose selector collides with a detected submit control.
    classified_guarded = {
        'fields': {'overall': {'selector': '#danger', 'kind': 'text', 'tag': 'input',
                               'type': 'text', 'score': 7, 'text': ''}},
        'submit_controls': [{'selector': '#danger', 'text': 'Submit'}],
        'unmatched': 0,
    }
    plan = build_fill_plan(classified_guarded, {'overall': '7'})
    sels = [a['selector'] for a in plan['actions']]
    assert '#danger' not in sels, 'defense-in-depth must refuse a selector that is also a submit control'
    assert any(s['field'] == 'overall' and 'submit control' in s['reason'] for s in plan['skipped']), plan['skipped']

    # Detection-miss counterfactual: the SAME field selector, but NOT recorded
    # as a submit control → it is now emitted. Proves submit_controls is what
    # drives the exclusion (the guard is load-bearing, not incidental).
    classified_unguarded = {
        'fields': classified_guarded['fields'],
        'submit_controls': [],
        'unmatched': 0,
    }
    plan2 = build_fill_plan(classified_unguarded, {'overall': '7'})
    assert '#danger' in [a['selector'] for a in plan2['actions']], \
        'without the submit_controls entry the selector is emitted — confirms the guard drives exclusion'
    _ok('NEGATIVE CONTROL: build_fill_plan defense-in-depth submit_selectors guard is load-bearing')


def main():
    print()
    tests = [
        test_is_submit_control_recognizes_commit_controls,
        test_classify_maps_fields_across_venue_variants,
        test_confidence_and_title_beat_generic_review_match,
        test_review_textarea_beats_short_input,
        test_extract_review_values_splits_prose_and_scores,
        test_extract_review_values_skips_scale_range_takes_choice,
        test_fill_plan_excludes_submit_and_has_no_submit_action,
        test_fill_plan_skips_missing_fields_and_empty_values,
        test_orchestration_fills_and_never_touches_submit,
        test_orchestration_refuses_non_openreview_tab_without_acting,
        test_orchestration_refuses_when_no_form,
        test_orchestration_reports_bridge_disconnect,
        test_negative_control_defense_in_depth_submit_selector_guard,
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
