"""Server-side target resolution for the current browser tool surface.

Everything here is work the MODEL used to do by hand, moved into code
( — the "handle as much as we can through code"
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
   so verification no longer costs a whole extra LLM round. v3
   (2026-08-05): the receipt also diffs the tab-ID SET, so a click that
   opened a NEW TAB (target=_blank card UIs — the 钱管家 incident, conv
   msft42tqheea8x) is reported and auto-followed instead of looking like
   "nothing happened". Entirely server-side: works with every extension
   version, no extension update required.

Every stateful function requires an explicit ``(owner_user_id, client_id)``
route key and every bridge operation requires an injected sender. Browser
state can therefore never cross users or devices through a module global.
"""

import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'remember_work_tab', 'forget_work_tab', 'resolve_work_tab',
    'current_work_tab', 'resolve_element', 'auto_wait', 'action_receipt',
]


# ══════════════════════════════════════════════════════════
#  1. Working-tab memory
# ══════════════════════════════════════════════════════════

_work_tab_lock = threading.Lock()
_work_tabs: dict[tuple[str, str], int] = {}


def _normalize_route_key(route_key) -> tuple[str, str]:
    if not isinstance(route_key, tuple) or len(route_key) != 2:
        raise ValueError('browser route_key must be (owner_user_id, client_id)')
    owner_user_id = str(route_key[0] or '').strip()
    client_id = str(route_key[1] or '').strip()
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    if not client_id:
        raise ValueError('client_id is required')
    return owner_user_id, client_id


def remember_work_tab(route_key, tab_id):
    """Record the tab a tool just acted on (or created)."""
    if tab_id is None:
        return
    try:
        tab_id = int(tab_id)
    except (TypeError, ValueError):
        logger.debug('Non-numeric work tab id ignored: %s', tab_id)
        return
    route_key = _normalize_route_key(route_key)
    with _work_tab_lock:
        _work_tabs[route_key] = tab_id


def forget_work_tab(route_key, tab_id):
    """Drop the remembered tab if it is the one being closed."""
    try:
        tab_id = int(tab_id)
    except (TypeError, ValueError) as e:
        logger.debug('Non-numeric work tab id ignored on forget: %s (%s)',
                     tab_id, e)
        return
    route_key = _normalize_route_key(route_key)
    with _work_tab_lock:
        if _work_tabs.get(route_key) == tab_id:
            _work_tabs.pop(route_key, None)


def current_work_tab(route_key):
    """Return the remembered working-tab id (int) or None — no bridge call.

    The route key prevents one owner's tab choice from influencing another.
    """
    route_key = _normalize_route_key(route_key)
    with _work_tab_lock:
        return _work_tabs.get(route_key)


def resolve_work_tab(fn_args, *, route_key, send):
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
        remember_work_tab(route_key, tab_id)
        return tab_id
    route_key = _normalize_route_key(route_key)
    with _work_tab_lock:
        current = _work_tabs.get(route_key)
    if current is not None:
        return current
    try:
        result, error = send('list_tabs', timeout=10)
    except Exception as e:
        logger.debug('work-tab seed list_tabs raised: %s', e)
        return None
    if error or not isinstance(result, list) or not result:
        return None
    for t in result:
        if t.get('active') and t.get('id') is not None:
            remember_work_tab(route_key, t['id'])
            return int(t['id'])
    if result[0].get('id') is not None:
        remember_work_tab(route_key, result[0]['id'])
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
        # extension >= 4.8.0 flags cursor:pointer roots (React/Vue card UIs
        # expose no semantic tell at all) with pointer=True — rank them just
        # below real buttons/links, above plain divs.
        boost = (15 if (tag in _CLICK_TAGS or role in _CLICK_ROLES)
                 else (10 if el.get('pointer') else 0))
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


def resolve_element(tab_id, query, kinds='clickable', *, send):
    """Resolve a natural-language target to a concrete element.

    Returns ``(element, error_note, candidates)``: exactly one of element /
    error_note is set. ``candidates`` is a short human-readable list of the
    closest matches, meant to be embedded in the error so the model's next
    call hits (self-healing failure).
    """
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

def auto_wait(tab_id, selector, *, send, timeout_ms=3000):
    """Wait briefly for the selector to exist before acting on it.

    Advisory only: returns '' when the element is present, otherwise a short
    bracketed note for the result line — the action itself is NEVER blocked
    (the extension's click scrolls/retries on its own, and a wait false
    negative must not break a click that would have worked).
    """
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

def tab_snapshot(tab_id, *, send):
    """Pre-action snapshot: ``(title, url, tab-id set)``.

    The id set is what lets :func:`action_receipt` spot a tab the ACTION
    opened — a click on a target=_blank card creates a new tab WITHOUT
    touching the old one, so comparing only the old tab's URL reports
    "nothing happened" while the result page sits open next door. Costs one
    cheap list_tabs; on any failure the id set is None and the receipt
    degrades to the pre-v3 title/URL-only comparison.
    """
    title, url = None, None
    try:
        result, error = send('list_tabs', timeout=8)
    except Exception as e:
        logger.debug('tab_snapshot list_tabs raised (tab=%s): %s', tab_id, e)
        return title, url, None
    if error or not isinstance(result, list):
        return title, url, None
    for t in result:
        if t.get('id') is not None and str(t.get('id')) == str(tab_id):
            title = t.get('title') or title
            url = t.get('url') or url
            break
    ids = {t.get('id') for t in result if t.get('id') is not None}
    return title, url, ids


def _settle(seconds):
    """Sleep seam for the new-tab settle retry (tests patch this)."""
    import time
    time.sleep(seconds)


def _unsettled(tab):
    """A tab just born via target=_blank still shows an empty/placeholder
    URL — its real destination arrives moments later."""
    url = (tab.get('url') or '').strip()
    return not url or url == 'about:blank' or url.startswith('chrome://')


def _pick_new_tabs(before_ids, tabs):
    """(chosen, extra_count) among tabs whose ids are not in before_ids.

    Prefers the browser-focused new tab (target=_blank opens active), else
    the most recently created (highest id)."""
    new = [t for t in tabs
           if t.get('id') is not None and t.get('id') not in before_ids]
    if not new:
        return None, 0
    chosen = next((t for t in new if t.get('active')), new[-1])
    return chosen, len(new) - 1


def action_receipt(tab_id, before, *, route_key, send):
    """One-line post-action page-state delta.

    Compares the tab's title/URL after the action against the ``before``
    snapshot (from :func:`tab_snapshot`, captured pre-action), so the model
    learns "the click navigated / submitted / opened a new tab / did
    nothing visible" WITHOUT spending another LLM round on a verification
    read. Costs one cheap list_tabs bridge call; degrades to '' on any
    failure.

    When the tab-id set grew, the action opened a tab.
    The new tab becomes the working tab (that is where a human's attention
    goes — Chrome focuses it too) and the receipt says so explicitly, so
    the next tab_id-less call lands on the new page for this owner/device.

    Known limit: an ASYNC window.open — one fired from an XHR callback
    hundreds of ms after the click returns — can slip the diff window (the
    receipt list_tabs runs before the tab exists). The synchronous
    target=_blank case that motivated v3 is covered; a delayed open is
    still discoverable via browser_list_tabs / the next action's receipt.
    """
    route_key = _normalize_route_key(route_key)
    if not isinstance(before, tuple) or len(before) != 3:
        raise ValueError('browser action snapshot must be a 3-tuple')
    try:
        result, error = send('list_tabs', timeout=8)
    except Exception as e:
        logger.debug('action_receipt list_tabs raised (tab=%s): %s', tab_id, e)
        return ''
    if error or not isinstance(result, list):
        return ''
    before_title, before_url, before_ids = before

    parts = []
    if before_ids is not None:
        chosen, extras = _pick_new_tabs(before_ids, result)
        if chosen is not None and _unsettled(chosen):
            # One bounded settle retry so the receipt names the REAL
            # destination instead of 'about:blank'.
            _settle(1.2)
            try:
                again, err2 = send('list_tabs', timeout=8)
            except Exception as e:
                logger.debug('action_receipt settle re-list raised: %s', e)
                again, err2 = None, 'settle re-list raised'
            if not err2 and isinstance(again, list):
                refresh = next((t for t in again
                                if t.get('id') == chosen.get('id')), None)
                if refresh is not None:
                    chosen = refresh
        if chosen is not None:
            new_id = chosen.get('id')
            new_title, new_url = chosen.get('title', ''), chosen.get('url', '')
            remember_work_tab(route_key, new_id)
            where = f' — {new_title} ({new_url})' if (new_title or new_url) else ''
            more = f' (+{extras} more new tabs)' if extras else ''
            parts.append(
                f'→ the action opened a NEW TAB #{new_id}{where}{more}. '
                f'It is now the working tab; tab #{tab_id} stays open.')

    current = None
    for t in result:
        if t.get('id') is not None and str(t.get('id')) == str(tab_id):
            current = t
            break
    if current is None:
        parts.append('→ note: the tab no longer exists (closed or crashed)')
        return '\n' + '\n'.join(parts)
    title, url = current.get('title', ''), current.get('url', '')
    if before_url and url and url != before_url:
        parts.append(f'→ page navigated: {title} ({url})')
    elif before_url and url == before_url:
        if title and before_title and title != before_title:
            parts.append(f'→ same URL, new title: {title}')
        elif not parts:
            # 'same page' is only worth saying when nothing ELSE happened —
            # next to a NEW TAB line it is noise.
            parts.append('→ same page (URL unchanged)')
    elif url:
        parts.append(f'→ now on: {title} ({url})')
    # Handlers append the receipt straight onto the result line — the
    # leading newline is the historical separator contract.
    return ('\n' + '\n'.join(parts)) if parts else ''
