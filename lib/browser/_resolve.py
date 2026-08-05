"""lib/browser/_resolve.py — Server-side intelligence for the v2 tool surface.

Everything here is work the MODEL used to do by hand, moved into code
(epic pt_869e5648403e4745 — the "handle as much as we can through code"
half of the v2 surface):

1. **Working-tab memory** — the last tab a tool acted on is remembered, so
   ``tab_id`` is optional everywhere: omit it and the action lands on the
   tab you were already working with (seeded from the browser's active tab
   on first use). This kills the list_tabs-first ceremony for single-tab
   flows and the per-call id plumbing.
2. **Intent-based element resolution** — ``text=`` params are resolved
   server-side: enumerate interactive elements once, fuzzy-rank them
   (exact > prefix > substring; role/tag-aware boost), act on the winner.
   On ambiguity the candidates are returned so the model's NEXT call
   succeeds — CSS selectors stop being something the model has to invent.
3. **Auto-wait** — a Playwright-style presence wait before acting on a
   model-supplied selector. It never blocks the action; it annotates.
4. **Action receipts** — after a click/type/navigation, one cheap
   ``list_tabs`` tells the model whether the page changed (URL/title),
   so verification no longer costs a whole extra LLM round.

All functions take an optional ``send`` callable (the bridge command
dispatcher) so callers keep their own monkeypatch contract — handlers pass
their facade proxy, advanced.py passes its module-level name, and tests
inject fakes directly. The default is lib.browser.queue.send_browser_command.
"""

import threading

from lib.browser.display import get_tab_title, get_tab_url, update_tab_title
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'remember_work_tab', 'forget_work_tab', 'resolve_work_tab',
    'resolve_element', 'auto_wait', 'action_receipt',
]


def _default_send():
    from lib.browser.queue import send_browser_command
    return send_browser_command


# ══════════════════════════════════════════════════════════
#  1. Working-tab memory
# ══════════════════════════════════════════════════════════

_work_tab_lock = threading.Lock()
_work_tab = {'id': None}


def remember_work_tab(tab_id):
    """Record the tab a tool just acted on (or created)."""
    if tab_id is None:
        return
    try:
        tab_id = int(tab_id)
    except (TypeError, ValueError):
        logger.debug('Non-numeric work tab id ignored: %s', tab_id)
        return
    with _work_tab_lock:
        _work_tab['id'] = tab_id


def forget_work_tab(tab_id):
    """Drop the remembered tab if it is the one being closed."""
    try:
        tab_id = int(tab_id)
    except (TypeError, ValueError) as e:
        logger.debug('Non-numeric work tab id ignored on forget: %s (%s)',
                     tab_id, e)
        return
    with _work_tab_lock:
        if _work_tab['id'] == tab_id:
            _work_tab['id'] = None


def resolve_work_tab(fn_args, send=None):
    """Resolve which tab this call should act on. Returns int tab id or None.

    Priority: explicit ``tabId`` arg (which also becomes the new working
    tab) → remembered working tab → the browser's currently active tab
    (seeded via one list_tabs call) → None (caller renders the error).
    """
    explicit = fn_args.get('tabId')
    if explicit is not None:
        try:
            tab_id = int(explicit)
        except (TypeError, ValueError) as e:
            logger.debug('Non-numeric explicit tabId ignored: %s (%s)',
                         explicit, e)
            return None
        remember_work_tab(tab_id)
        return tab_id
    with _work_tab_lock:
        current = _work_tab['id']
    if current is not None:
        return current
    send = send or _default_send()
    try:
        result, error = send('list_tabs', timeout=10)
    except Exception as e:
        logger.debug('work-tab seed list_tabs raised: %s', e)
        return None
    if error or not isinstance(result, list) or not result:
        return None
    for t in result:
        if t.get('active') and t.get('id') is not None:
            remember_work_tab(t['id'])
            return int(t['id'])
    if result[0].get('id') is not None:
        remember_work_tab(result[0]['id'])
        return int(result[0]['id'])
    return None


# ══════════════════════════════════════════════════════════
#  2. Intent-based element resolution (text= → selector)
# ══════════════════════════════════════════════════════════

_CLICK_TAGS = {'a', 'button'}
_CLICK_ROLES = {'button', 'link', 'menuitem', 'tab', 'option', 'checkbox',
                'radio', 'switch'}
_INPUT_TAGS = {'input', 'textarea', 'select'}
_INPUT_ROLES = {'textbox', 'combobox', 'searchbox', 'spinbutton'}


def _norm(s):
    return ' '.join(str(s or '').split()).lower()


def _score_element(el, query, kinds):
    """Rank one element against the query. 0 = no match; higher = better."""
    q = _norm(query)
    if not q or el.get('disabled'):
        return 0
    tag = _norm(el.get('tag'))
    role = _norm(el.get('role'))
    if kinds == 'input':
        if tag not in _INPUT_TAGS and role not in _INPUT_ROLES:
            return 0
        # Inputs carry their label in placeholder/aria/title, not inner text.
        fields = ((el.get('placeholder'), 0), (el.get('ariaLabel'), 0),
                  (el.get('title'), 0), (el.get('text'), -10))
        boost = 15 if (tag in _INPUT_TAGS or role in _INPUT_ROLES) else 0
    else:
        fields = ((el.get('text'), 0), (el.get('ariaLabel'), -5),
                  (el.get('title'), -5))
        boost = 15 if (tag in _CLICK_TAGS or role in _CLICK_ROLES) else 0
    best = 0
    for value, penalty in fields:
        v = _norm(value)
        if not v:
            continue
        if v == q:
            best = max(best, 100 + penalty)
        elif v.startswith(q):
            best = max(best, 75 + penalty)
        elif q in v:
            best = max(best, 55 + penalty)
    return best + boost if best else 0


def resolve_element(tab_id, query, kinds='clickable', send=None):
    """Resolve a natural-language target to a concrete element.

    Returns ``(element, error_note, candidates)``: exactly one of element /
    error_note is set. ``candidates`` is a short human-readable list of the
    closest matches, meant to be embedded in the error so the model's next
    call hits (self-healing failure).
    """
    send = send or _default_send()
    try:
        result, error = send('get_interactive_elements', {
            'tabId': int(tab_id), 'viewport': False, 'maxElements': 300,
        }, timeout=15)
    except Exception as e:
        logger.warning('resolve_element discovery raised (tab=%s): %s', tab_id, e)
        return None, f'element discovery failed: {e}', []
    if error:
        return None, f'element discovery failed: {error}', []
    elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
    scored = [(_score_element(el, query, kinds), el) for el in elements]
    scored = [(s, el) for s, el in scored if s > 0]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        fallback = [_candidate_line(el) for el in elements[:6]]
        return None, f'no element matches "{query}"', fallback
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score >= 55 and best_score - second_score >= 10:
        return best, None, []
    candidates = [_candidate_line(el) for _s, el in scored[:6]]
    return None, f'"{query}" is ambiguous ({len(scored)} matches)', candidates


def _candidate_line(el):
    tag = el.get('tag', '?')
    text = (el.get('text') or el.get('ariaLabel') or el.get('placeholder')
            or el.get('title') or '')
    sel = el.get('selector', '')
    return f'  <{tag}> "{text[:60]}" selector: {sel}'


# ══════════════════════════════════════════════════════════
#  3. Auto-wait (Playwright-style, advisory)
# ══════════════════════════════════════════════════════════

def auto_wait(tab_id, selector, send=None, timeout_ms=3000):
    """Wait briefly for the selector to exist before acting on it.

    Advisory only: returns '' when the element is present, otherwise a short
    bracketed note for the result line — the action itself is NEVER blocked
    (the extension's click scrolls/retries on its own, and a wait false
    negative must not break a click that would have worked).
    """
    send = send or _default_send()
    try:
        result, error = send('wait_for_element', {
            'tabId': int(tab_id), 'selector': selector,
            'condition': 'present', 'timeout': timeout_ms,
        }, timeout=timeout_ms / 1000 + 3)
    except Exception as e:
        logger.debug('auto_wait raised (tab=%s selector=%s): %s', tab_id, selector, e)
        return ''
    if error:
        return f' [pre-action wait failed: {error}]'
    if isinstance(result, dict) and (result.get('found') or result.get('success')):
        return ''
    return f' [element not present after {timeout_ms}ms wait — acted anyway]'


# ══════════════════════════════════════════════════════════
#  4. Action receipts — post-action page-state delta
# ══════════════════════════════════════════════════════════

def tab_snapshot(tab_id):
    """(title, url) currently cached for the tab, pre-action."""
    return get_tab_title(tab_id), get_tab_url(tab_id)


def action_receipt(tab_id, before, send=None):
    """One-line post-action page-state delta.

    Compares the tab's title/URL after the action against the ``before``
    snapshot (from :func:`tab_snapshot`, captured pre-action), so the model
    learns "the click navigated / submitted / did nothing visible" WITHOUT
    spending another LLM round on a verification read. Costs one cheap
    list_tabs bridge call; degrades to '' on any failure.
    """
    send = send or _default_send()
    try:
        result, error = send('list_tabs', timeout=8)
    except Exception as e:
        logger.debug('action_receipt list_tabs raised (tab=%s): %s', tab_id, e)
        return ''
    if error or not isinstance(result, list):
        return ''
    current = None
    for t in result:
        if t.get('id') is not None and str(t.get('id')) == str(tab_id):
            current = t
            break
    if current is None:
        return '\n→ note: the tab no longer exists (closed or crashed)'
    title, url = current.get('title', ''), current.get('url', '')
    update_tab_title(tab_id, title, url=url)
    before_title, before_url = before
    if before_url and url and url != before_url:
        return f'\n→ page navigated: {title} ({url})'
    if before_url and url == before_url:
        if title and before_title and title != before_title:
            return f'\n→ same URL, new title: {title}'
        return '\n→ same page (URL unchanged)'
    if url:
        return f'\n→ now on: {title} ({url})'
    return ''
