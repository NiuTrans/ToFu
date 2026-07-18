"""lib/paper/openreview_autofill.py — OpenReview single-page auto-fill (killer feature).

Given ONE OpenReview submission page the reviewer is looking at, this:
  1. classifies the review-form fields from the bridge's ``get_interactive_elements``
     dump (venue-agnostic — OpenReview forms differ per venue, so we match by
     label/name/placeholder heuristics, never a hard-coded column list);
  2. maps our generated Review-Mode output (review prose + OA + confidence) onto
     those fields;
  3. builds a FILL PLAN of ``type_text`` / ``click_element`` actions the bridge
     executes — and **structurally excludes every submit / confirm / post
     control**, so the flow always stops at "filled, awaiting the human".

## Why the guardrail lives HERE (a pure function), not in the orchestrator
"Never click the final Submit" is the single most important safety property of
this feature. If it lived only in the imperative orchestration code it would be
one stray ``click_element`` away from posting a review on the user's behalf.
Instead, :func:`build_fill_plan` is a pure function that (a) only ever emits
``fill``/``click`` actions for fields it POSITIVELY classified as review inputs,
and (b) runs every candidate through :func:`is_submit_control` and drops it.
:func:`plan_has_submit_action` re-audits the finished plan. Both are unit-tested
directly (see tests/test_openreview_autofill.py), so the guarantee is a tested
invariant, not a code-review promise.

The orchestration entry (:func:`autofill_openreview_review`) is deliberately
thin and delegates every decision to the pure functions.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)


# ── Submit / confirm control detection (the load-bearing guardrail) ─────
# Any element whose visible text / name / aria-label / id matches one of these
# is treated as a "commit" control and is NEVER emitted into a fill plan. Kept
# deliberately broad: a false positive only means the human clicks it themselves
# (safe); a false negative could auto-post a review (catastrophic). When in
# doubt, it is a submit control.
_SUBMIT_TOKENS = (
    'submit', 'post ', 'post review', 'post comment', 'confirm', 'send',
    'publish', 'finish', 'save review', 'save & submit', 'save and submit',
    'complete review', 'finalize', 'finalise',
)
# Button-ish roles that could commit the form. ``a`` (link) can also submit.
_CLICKABLE_TAGS = {'button', 'a', 'summary'}


def _norm(s):
    """Lowercased, whitespace-collapsed string (never raises)."""
    return re.sub(r'\s+', ' ', str(s or '')).strip().lower()


def _el_labels(el):
    """All human-facing label strings on an element, lowercased.

    OpenReview renders the field label variously as the input's ``name``,
    ``aria-label``, ``placeholder``, ``title``, or nearby ``text``. We match
    against ALL of them so the classifier is robust across venue form variants.
    """
    parts = [el.get('text'), el.get('name'), el.get('ariaLabel'),
             el.get('placeholder'), el.get('title'), el.get('label')]
    return [_norm(p) for p in parts if p]


def is_submit_control(el):
    """True when ``el`` is a submit/confirm/post control that must NOT be auto-clicked.

    Broad by design: matches a clickable element (button/link/summary or an
    ``input[type=submit|button]``) whose any label contains a commit token. A
    plain text ``input``/``textarea``/``select`` is never a submit control
    (typing into it commits nothing).

    Args:
        el: one interactive-element dict from ``get_interactive_elements``.

    Returns:
        bool — True if the element could commit the form.
    """
    tag = _norm(el.get('tag'))
    typ = _norm(el.get('type'))
    role = _norm(el.get('role'))
    # An input[type=submit|image] is inherently a form-commit control — treat it
    # as submit UNCONDITIONALLY, regardless of its label (its whole job is to
    # submit). This is the safe direction: never type into / auto-act on it.
    if tag == 'input' and typ in ('submit', 'image'):
        return True
    # Text-entry controls never submit on their own.
    if tag == 'textarea' or (tag == 'input' and typ in ('text', 'number', 'search', 'email', '', 'url')):
        return False
    is_clickable = (tag in _CLICKABLE_TAGS or role in ('button', 'link')
                    or typ in ('submit', 'button', 'image'))
    if not is_clickable:
        return False
    labels = _el_labels(el)
    hay = ' '.join(labels)
    return any(tok in hay for tok in _SUBMIT_TOKENS)


# ── Review-field classification (venue-agnostic) ────────────────────────
# Each logical field is matched by keyword groups against the element labels.
# ``kind`` drives the fill action: 'text' → type the value; 'rating' → the value
# is a score whose option must be matched among select/radio options.
_FIELD_MATCHERS = (
    # (field_key, kind, [keyword phrases, matched as substrings])
    ('title',      'text',   ['review title', 'title']),
    ('review',     'text',   ['review', 'main review', 'detailed comments',
                              'comments to authors', 'comment']),
    # OA / overall — many aliases across venues.
    ('overall',    'rating', ['overall assessment', 'overall rating', 'overall',
                              'recommendation', 'rating', 'oa', 'score']),
    ('confidence', 'rating', ['confidence']),
)

# When several matchers could fire on one element, prefer the MOST specific
# (longest matched phrase). 'confidence' must beat 'review'/'overall'; 'title'
# must beat 'review' for a "Review Title" field.
def _best_field(el):
    """Return (field_key, kind, score) for the best-matching review field, or None.

    ``score`` is the length of the longest keyword phrase that matched — longer
    (more specific) phrases win, so "review title" classifies as ``title`` not
    ``review``, and "confidence" never collapses into the generic ``review``.
    """
    labels = _el_labels(el)
    if not labels:
        return None
    best = None
    for field_key, kind, phrases in _FIELD_MATCHERS:
        for ph in phrases:
            if any(ph in lb for lb in labels):
                score = len(ph)
                if best is None or score > best[2]:
                    best = (field_key, kind, score)
    return best


def classify_review_form(elements):
    """Map an interactive-elements dump to review-form fields (venue-agnostic).

    Only text-entry (``input[text/number]`` / ``textarea``) and choice
    (``select`` / radio / a rating button-group) controls are considered
    fillable. Submit/confirm controls are recorded SEPARATELY (never fillable)
    so the caller can prove they were excluded.

    Args:
        elements: the ``elements`` list from ``get_interactive_elements``.

    Returns:
        dict: ``{
          'fields': {field_key: {selector, kind, tag, options?, el}},
          'submit_controls': [ {selector, text} ],   # detected, NEVER filled
          'unmatched': int,
        }`` — ``fields`` keeps only the best selector per logical field.
    """
    fields = {}
    submit_controls = []
    unmatched = 0
    for el in (elements or []):
        selector = el.get('selector') or ''
        if not selector:
            unmatched += 1
            continue
        if is_submit_control(el):
            submit_controls.append({'selector': selector,
                                     'text': el.get('text') or el.get('name') or ''})
            continue
        tag = _norm(el.get('tag'))
        typ = _norm(el.get('type'))
        fillable = (tag == 'textarea'
                    or tag == 'select'
                    or (tag == 'input' and typ in ('text', 'number', 'search', '', 'radio'))
                    or _norm(el.get('role')) in ('radio', 'option', 'combobox', 'listbox'))
        if not fillable:
            unmatched += 1
            continue
        match = _best_field(el)
        if not match:
            unmatched += 1
            continue
        field_key, kind, score = match
        # A textarea always wins the 'review' slot over a one-line input.
        prev = fields.get(field_key)
        cand = {'selector': selector, 'kind': kind, 'tag': tag,
                'type': typ, 'score': score, 'text': el.get('text') or el.get('name') or ''}
        if prev is None or _field_candidate_better(field_key, cand, prev):
            fields[field_key] = cand
    return {'fields': fields, 'submit_controls': submit_controls, 'unmatched': unmatched}


def _field_candidate_better(field_key, cand, prev):
    """Tie-break two elements matched to the same field.

    For the free-text ``review`` field a ``textarea`` beats a short ``input``.
    Otherwise the higher keyword-specificity score wins; ties keep the first.
    """
    if field_key == 'review':
        if cand['tag'] == 'textarea' and prev['tag'] != 'textarea':
            return True
        if cand['tag'] != 'textarea' and prev['tag'] == 'textarea':
            return False
    return cand['score'] > prev['score']


# ── Fill-plan construction (structurally submit-free) ───────────────────

def build_fill_plan(classified, values):
    """Build the ordered list of bridge actions to FILL the review form.

    The plan contains ONLY ``fill`` (type_text) and ``click`` (rating option)
    actions for POSITIVELY-classified review fields. It NEVER contains an action
    that targets a submit/confirm control: submit controls are not in
    ``classified['fields']`` at all, and as defense-in-depth every action's
    selector is re-checked so it cannot coincide with a detected submit control.

    Args:
        classified: output of :func:`classify_review_form`.
        values: ``{'title', 'review', 'overall', 'confidence'}`` strings from the
            generated review (any missing/empty value is skipped).

    Returns:
        dict: ``{'actions': [ {op:'fill'|'click', field, selector, value?} ],
                 'skipped': [ {field, reason} ]}``. ``op`` is only ever
        'fill'/'click' — there is no 'submit' op in this system.
    """
    fields = (classified or {}).get('fields', {})
    submit_selectors = {c['selector'] for c in (classified or {}).get('submit_controls', [])}
    actions = []
    skipped = []
    # Deterministic order: title, review, overall, confidence.
    for field_key in ('title', 'review', 'overall', 'confidence'):
        value = (values or {}).get(field_key)
        info = fields.get(field_key)
        if not value:
            continue
        if not info:
            skipped.append({'field': field_key, 'reason': 'no matching form field found'})
            continue
        selector = info['selector']
        # Defense-in-depth: refuse to ever act on a submit control's selector.
        if selector in submit_selectors:
            skipped.append({'field': field_key, 'reason': 'selector collides with a submit control — refused'})
            continue
        if info['kind'] == 'rating':
            # A score field: type it if it is a text/number input; otherwise the
            # orchestrator will resolve the option among the control's choices
            # (op still 'click', never 'submit').
            if info['tag'] in ('input',) and info.get('type') in ('text', 'number', '', 'search'):
                actions.append({'op': 'fill', 'field': field_key, 'selector': selector, 'value': str(value)})
            else:
                actions.append({'op': 'click', 'field': field_key, 'selector': selector,
                                'value': str(value), 'resolve_option': True})
        else:
            actions.append({'op': 'fill', 'field': field_key, 'selector': selector, 'value': str(value)})
    return {'actions': actions, 'skipped': skipped}


def plan_has_submit_action(plan):
    """Audit a fill plan for ANY submit/commit action. Must always be False.

    Belt-and-suspenders check the orchestrator asserts before executing: the
    plan may only contain 'fill'/'click' ops. A future refactor that somehow
    introduced a submit op (or an action flagged commit) trips this and the
    whole autofill aborts rather than risk posting.

    Returns:
        bool — True if the plan contains any submit/commit-shaped action.
    """
    for a in (plan or {}).get('actions', []):
        op = _norm(a.get('op'))
        if op not in ('fill', 'click'):
            return True
        if a.get('submit') or a.get('commit') or a.get('post'):
            return True
    return False


# ── OpenReview page identification ──────────────────────────────────────
_FORUM_ID_RE = re.compile(r'[?&](?:id|noteId|forum)=([A-Za-z0-9_\-]+)')


def is_openreview_url(url):
    """True when ``url`` is an openreview.net page."""
    u = _norm(url)
    return 'openreview.net' in u


def extract_forum_id(url):
    """Pull the OpenReview forum / note id from a submission URL, or ''.

    Handles ``…/forum?id=XXXX`` and ``…?noteId=XXXX`` shapes.
    """
    m = _FORUM_ID_RE.search(str(url or ''))
    return m.group(1) if m else ''


def extract_pdf_url(elements, page_url=''):
    """Find the paper PDF link on the OpenReview page, or ''.

    OpenReview exposes the PDF as ``/pdf?id=XXXX`` (a link) or ``/attachment``.
    We scan the interactive elements' hrefs; fall back to deriving ``/pdf?id=``
    from the forum id in the page URL.
    """
    for el in (elements or []):
        href = _norm(el.get('href'))
        if not href:
            continue
        if '/pdf?id=' in href or href.endswith('.pdf') or '/attachment' in href:
            return el.get('href')
    fid = extract_forum_id(page_url)
    if fid:
        return f'https://openreview.net/pdf?id={fid}'
    return ''


# ── Extracting fillable values from a finished review body ──────────────
# The stored review is: <prose body>\n\n--- FOR THE REVIEW FORM … ---\n\n<scorecard>.
# The review TEXT to paste is the prose body (above the separator). The OA /
# confidence NUMBERS the reviewer chose are in the scorecard block. The model is
# instructed to end each score line with the chosen value, e.g.
# ``- **Overall Assessment (OA)**: 5 — accept, good paper``; we pull the FIRST
# number on the OA/Confidence line. Robust to missing scorecard (returns '').
_OA_LINE_RE = re.compile(
    r'^[ \t>*_-]*\**[ \t]*(?:overall(?:\s+assessment)?(?:\s*\(oa\))?|oa|overall rating|recommendation)\b[^\n]*',
    re.IGNORECASE | re.MULTILINE)
_CONF_LINE_RE = re.compile(
    r'^[ \t>*_-]*\**[ \t]*confidence\b[^\n]*',
    re.IGNORECASE | re.MULTILINE)
# The chosen score on a scorecard line: the first standalone integer that is NOT
# part of a scale range like "1–6" / "1-10" (those describe the scale, not the
# choice). We take the number AFTER a ``:`` / ``：`` if present, else the first
# integer that is not immediately followed by an en/em dash + digit.
_SCORE_PICK_RE = re.compile(r'[:：]\s*\**\s*(\d+)')
_ANY_INT_RE = re.compile(r'(?<![\d.])(\d+)(?![\u2013\u2014\-]\d)')


def _pick_score(line):
    """Extract the reviewer's chosen integer score from a scorecard line, or ''.

    Prefers the number right after the ``:`` (``OA: 5 — …`` → ``5``); falls back
    to the first integer that is not the left end of a scale range (so ``1–6``
    is skipped but a lone ``5`` is taken).
    """
    if not line:
        return ''
    m = _SCORE_PICK_RE.search(line)
    if m:
        return m.group(1)
    # Drop scale ranges (``1–6``, ``1-10``) then take the first remaining int.
    stripped = re.sub(r'\d+\s*[\u2013\u2014\-]\s*\d+', ' ', line)
    m2 = re.search(r'\b(\d+)\b', stripped)
    return m2.group(1) if m2 else ''


def extract_review_values(review_body, title=''):
    """Split a finished review into the fillable {title, review, overall, confidence}.

    Args:
        review_body: the stored/finalized review Markdown (prose + separator +
            scorecard).
        title: optional review title (e.g. paper title) for the title field.

    Returns:
        dict: ``{'title', 'review', 'overall', 'confidence'}``. ``review`` is the
        prose body ABOVE the form separator (never includes the scorecard, so a
        pasted review never leaks the scores into the free-text box). ``overall``
        / ``confidence`` are the chosen integers ('' if not found).
    """
    body = review_body or ''
    # Separate prose from scorecard using either language's separator.
    from lib.paper.review._textproc import _SCORECARD_SEPARATOR_EN, _SCORECARD_SEPARATOR_ZH
    prose, scorecard = body, ''
    for sep in (_SCORECARD_SEPARATOR_EN, _SCORECARD_SEPARATOR_ZH):
        if sep in body:
            prose, _, scorecard = body.partition(sep)
            break
    if not scorecard:
        # Not finalized yet: fall back to splitting at the scores heading.
        from lib.paper.review._textproc import _split_scorecard
        prose, scorecard = _split_scorecard(body)
    oa_m = _OA_LINE_RE.search(scorecard or '')
    conf_m = _CONF_LINE_RE.search(scorecard or '')
    return {
        'title': (title or '').strip(),
        'review': prose.strip(),
        'overall': _pick_score(oa_m.group(0) if oa_m else ''),
        'confidence': _pick_score(conf_m.group(0) if conf_m else ''),
    }


# ── Orchestration entry (thin — delegates to the pure functions above) ──

def autofill_openreview_review(bridge, review_values, client_id=None, timeout=20):
    """Fill the review form on the active OpenReview tab, then STOP (no submit).

    Thin orchestration over the pure classifier/planner. Steps:
      1. resolve the single active tab; require it to be an OpenReview page;
      2. ``get_interactive_elements`` → classify the review form;
      3. build a submit-free fill plan for ``review_values``;
      4. assert the plan has no submit action (:func:`plan_has_submit_action`);
      5. execute each fill/click via the bridge, gathering per-field results;
      6. return a report — never clicking any submit/confirm control.

    Args:
        bridge: module exposing ``send_browser_command(cmd_type, params, timeout,
            client_id)`` (dependency-injected so tests use a fake; production
            passes ``lib.browser``).
        review_values: ``{'title','review','overall','confidence'}`` strings.
        client_id: optional target extension client.
        timeout: per-command wait budget (seconds).

    Returns:
        dict: ``{'ok': bool, 'stage': str, 'filled': [...], 'skipped': [...],
                 'submit_controls_detected': int, 'message': str, 'tab'?, ...}``.
        ``ok`` False carries an actionable ``message`` (not connected / not an
        OpenReview page / no form / no PDF), never a silent failure.
    """
    def _send(cmd, params, to=None):
        return bridge.send_browser_command(cmd, params, timeout=to or timeout, client_id=client_id)

    # 1) Active tab.
    tabs, err = _send('list_tabs', {'active': True, 'currentWindow': True})
    if err:
        return {'ok': False, 'stage': 'connect',
                'message': f'Browser bridge not reachable: {err}'}
    if not tabs:
        return {'ok': False, 'stage': 'tab',
                'message': 'No active browser tab found. Open the OpenReview submission page and try again.'}
    tab = tabs[0] if isinstance(tabs, list) else tabs
    tab_id = tab.get('id')
    tab_url = tab.get('url') or ''
    if not is_openreview_url(tab_url):
        return {'ok': False, 'stage': 'not_openreview',
                'message': ('The active tab is not an OpenReview page. Open the paper\'s '
                            'OpenReview submission/forum page, then click auto-fill again.'),
                'tab': {'url': tab_url, 'title': tab.get('title', '')}}

    # 2) Interactive elements → classify.
    dump, err = _send('get_interactive_elements', {'tabId': tab_id, 'maxElements': 300, 'viewport': False})
    if err:
        return {'ok': False, 'stage': 'scan',
                'message': f'Could not read the OpenReview form: {err}'}
    elements = (dump or {}).get('elements', []) if isinstance(dump, dict) else []
    classified = classify_review_form(elements)
    if not classified['fields']:
        return {'ok': False, 'stage': 'no_form',
                'message': ('No review form was found on this page. Make sure the review '
                            'form is open (click "Official Review" / the review button first), '
                            'then retry.'),
                'submit_controls_detected': len(classified['submit_controls'])}

    # 3) Fill plan.
    plan = build_fill_plan(classified, review_values)

    # 4) Guardrail assertion — abort rather than risk a submit.
    if plan_has_submit_action(plan):
        logger.error('[OpenReview] Fill plan unexpectedly contained a submit action — ABORTING')
        return {'ok': False, 'stage': 'guardrail',
                'message': 'Internal safety check failed (a submit action was present) — aborted without touching the form.'}

    # 5) Execute fills only.
    filled = []
    for a in plan['actions']:
        if a['op'] == 'fill':
            res, ferr = _send('type_text', {'tabId': tab_id, 'selector': a['selector'],
                                            'text': a['value'], 'clearFirst': True,
                                            'pressEnter': False})
            filled.append({'field': a['field'], 'ok': ferr is None, 'error': ferr})
        elif a['op'] == 'click':
            res, ferr = _send('click_element', {'tabId': tab_id, 'selector': a['selector'],
                                                'scrollTo': True})
            filled.append({'field': a['field'], 'ok': ferr is None, 'error': ferr,
                           'note': 'rating control clicked; pick the exact option manually if needed'})

    ok_count = sum(1 for f in filled if f['ok'])
    return {
        'ok': ok_count > 0,
        'stage': 'filled',
        'filled': filled,
        'skipped': plan['skipped'],
        'submit_controls_detected': len(classified['submit_controls']),
        'tab': {'url': tab_url, 'title': tab.get('title', ''), 'id': tab_id},
        'message': (f'Filled {ok_count} field(s). The review is NOT submitted — '
                    'review the form and click Submit yourself when ready.'),
    }
