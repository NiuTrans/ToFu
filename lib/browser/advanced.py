"""Current compound browser interactions: menu selection and form fill."""

import base64
import json
import mimetypes
import os
import re
import time
import uuid
from typing import Any

from lib.browser._resolve import resolve_element
from lib.log import get_logger
from lib.runtime.runtime_paths import get_runtime_base

logger = get_logger(__name__)

__all__ = [
    'fill_form_single_pass', 'menu_click',
    'ADVANCED_BROWSER_TOOLS', 'ADVANCED_BROWSER_TOOL_NAMES',
    'ADVANCED_BROWSER_TOOL_FILL_FORM',
    'ADVANCED_BROWSER_TOOL_MENU_CLICK',
]


def _right_click_menu_select(
    tab_id: int,
    target_selector: str,
    menu_item_text: str,
    submenu_item_text: str | None = None,
    menu_wait: float = 0.5,
    timeout: float = 5.0,
    *,
    send,
) -> dict[str, Any]:
    """Right-click an element and select from the custom context menu.

    Steps: right-click target → wait for menu → find item → click → (optional) submenu.
    """
    start_time = time.time()
    steps = 0
    try:
        # Step 1: Get initial elements (validation)
        result, error = send('get_interactive_elements', {
            'tabId': tab_id, 'viewport': False, 'maxElements': 200
        }, timeout=min(timeout, 3))
        if error:
            return {'success': False, 'steps_completed': 0, 'error': f"Failed to get elements: {error}"}
        steps = 1

        # Step 2: Right-click target
        result, error = send('click_element', {
            'tabId': tab_id, 'selector': target_selector, 'rightClick': True, 'scrollTo': True
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'steps_completed': 1, 'error': f"Right-click failed: {error or 'Unknown error'}"}
        steps = 2

        # Step 3: Wait for menu
        time.sleep(menu_wait)
        steps = 3

        # Step 4: Get menu elements
        result, error = send('get_interactive_elements', {
            'tabId': tab_id, 'viewport': True, 'maxElements': 100
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error:
            return {'success': False, 'steps_completed': 3, 'error': f"Failed to get menu elements: {error}"}
        elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
        steps = 4

        # Step 5: Find menu item
        menu_item = None
        for el in elements:
            text = el.get('text', '').strip()
            if menu_item_text.lower() in text.lower():
                menu_item = el
                break
        if not menu_item:
            return {
                'success': False, 'steps_completed': 5,
                'error': f"Menu item '{menu_item_text}' not found",
                'available_items': [e.get('text', '') for e in elements[:20]]
            }

        # Step 6: Click menu item
        result, error = send('click_element', {
            'tabId': tab_id, 'selector': menu_item['selector'],
            'rightClick': False, 'scrollTo': False
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'steps_completed': 6, 'error': f"Click menu item failed: {error or 'Unknown error'}"}
        steps = 7

        # Step 7: Optional submenu
        if submenu_item_text:
            time.sleep(0.3)
            result, error = send('get_interactive_elements', {
                'tabId': tab_id, 'viewport': True, 'maxElements': 100
            }, timeout=min(timeout - (time.time() - start_time), 3))
            if error:
                return {'success': False, 'steps_completed': 7, 'error': f"Failed to get submenu elements: {error}"}
            elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
            steps = 8

            submenu_item = None
            for el in elements:
                if submenu_item_text.lower() in el.get('text', '').lower():
                    submenu_item = el
                    break
            if not submenu_item:
                return {
                    'success': False, 'steps_completed': 8,
                    'error': f"Submenu item '{submenu_item_text}' not found",
                    'available_items': [e.get('text', '') for e in elements[:20]]
                }
            result, error = send('click_element', {
                'tabId': tab_id, 'selector': submenu_item['selector'],
                'rightClick': False, 'scrollTo': False
            }, timeout=min(timeout - (time.time() - start_time), 3))
            if error or not (isinstance(result, dict) and result.get('clicked')):
                return {'success': False, 'steps_completed': 9, 'error': f"Click submenu item failed: {error or 'Unknown error'}"}
            steps = 9

        elapsed = time.time() - start_time
        return {
            'success': True, 'steps_completed': steps,
            'elapsed_ms': round(elapsed * 1000, 2),
            'details': {'target': target_selector, 'menu_item': menu_item_text, 'submenu_item': submenu_item_text}
        }
    except Exception as e:
        logger.warning('right_click_menu_select failed for target=%s menu_item=%s after %d steps: %s',
                       target_selector, menu_item_text, steps, e, exc_info=True)
        return {'success': False, 'steps_completed': steps, 'error': f"Exception: {str(e)}",
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


def menu_click(
    tab_id: int,
    item_text: str,
    target_selector: str | None = None,
    target_text: str | None = None,
    via: str = 'hover',
    submenu_item_text: str | None = None,
    menu_wait: float = 0.5,
    timeout: float = 5.0,
    *,
    send,
) -> dict[str, Any]:
    """Open a menu and click one visible item in a single call.

    via='right_click' opens a context menu; via='hover' (default) opens a
    hover menu, then runs the same
    text-match-and-click flow. The target may be named by target_text
    (fuzzy-resolved server-side) so the model never needs a selector for the
    common "open the File menu, click Export" case.
    """
    start_time = time.time()
    if not target_selector:
        if not target_text:
            return {'success': False,
                    'error': 'target_selector or target_text is required'}
        el, note, candidates = resolve_element(
            tab_id, target_text, 'clickable', send=send)
        if el is None:
            return {'success': False,
                    'error': f'menu target "{target_text}": {note}',
                    'available_items': [c.strip() for c in candidates]}
        target_selector = el.get('selector', '')
    if via == 'right_click':
        return _right_click_menu_select(
            tab_id, target_selector, item_text, submenu_item_text,
            menu_wait, timeout, send=send)
    # ── hover-open, then text-match the revealed item ──
    try:
        result, error = send('hover_element', {
            'tabId': tab_id, 'selector': target_selector,
        }, timeout=min(timeout, 3))
        hovered = isinstance(result, dict) and (
            result.get('hovered') or result.get('success'))
        if error or not hovered:
            return {'success': False,
                    'error': f'Hover failed: {error or (result or {}).get("error", "unknown")}'}
        time.sleep(menu_wait)
        result, error = send('get_interactive_elements', {
            'tabId': tab_id, 'viewport': True, 'maxElements': 100,
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error:
            return {'success': False,
                    'error': f'Failed to enumerate revealed menu: {error}'}
        elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
        menu_item = None
        for el in elements:
            if item_text.lower() in el.get('text', '').strip().lower():
                menu_item = el
                break
        if not menu_item:
            return {
                'success': False,
                'error': f"Menu item '{item_text}' not found",
                'available_items': [e.get('text', '') for e in elements[:20]],
            }
        result, error = send('click_element', {
            'tabId': tab_id, 'selector': menu_item['selector'], 'scrollTo': False,
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False,
                    'error': f"Click menu item failed: {error or 'unknown'}"}
        if submenu_item_text:
            time.sleep(0.3)
            result, error = send('get_interactive_elements', {
                'tabId': tab_id, 'viewport': True, 'maxElements': 100,
            }, timeout=min(timeout - (time.time() - start_time), 3))
            elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
            submenu = None
            for el in elements:
                if submenu_item_text.lower() in el.get('text', '').strip().lower():
                    submenu = el
                    break
            if not submenu:
                return {
                    'success': False,
                    'error': f"Submenu item '{submenu_item_text}' not found",
                    'available_items': [e.get('text', '') for e in elements[:20]],
                }
            result, error = send('click_element', {
                'tabId': tab_id, 'selector': submenu['selector'], 'scrollTo': False,
            }, timeout=min(timeout - (time.time() - start_time), 3))
            if error or not (isinstance(result, dict) and result.get('clicked')):
                return {'success': False,
                        'error': f"Click submenu item failed: {error or 'unknown'}"}
        return {
            'success': True,
            'elapsed_ms': round((time.time() - start_time) * 1000, 2),
            'details': {'via': 'hover', 'target': target_selector,
                        'menu_item': item_text,
                        'submenu_item': submenu_item_text},
        }
    except Exception as e:
        logger.warning('menu_click failed for target=%s item=%s: %s',
                       target_selector, item_text, e, exc_info=True)
        return {'success': False, 'error': f'Exception: {e}',
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


# ═══════════════════════════════════════════════════════
#  Single-pass form fill
#
#  The whole form is compiled into ONE async page script and executed through
#  a single execute_js bridge command.  Design contract (owner decree):
#  - ONE bridge round trip for N fields (the old sequential relay cost one
#    command per field and per dropdown, and models then looped
#    browser_click/browser_type around it).
#  - Every field is READ BACK and verified in-page; submit runs only when
#    every field verified — a half-filled form is never posted.
#  - Failures name the exact field plus observed options, so the minimal
#    repair is re-calling with ONLY the failed field(s) (clear-first makes
#    re-fills idempotent).
#  - The filled form's HTML is returned with the result and persisted to a
#    bounded per-owner snapshot file; the model inspects THAT file instead
#    of re-reading the live page into context.
# ═══════════════════════════════════════════════════════

#: Owner-scoped snapshot evidence for filled forms: transient, bounded,
#: pruned oldest-first on every write. Contents are never logged.
_SNAPSHOT_DIR = os.path.join(
    get_runtime_base(), 'data', 'browser_form_snapshots')
_SNAPSHOT_KEEP = 10            # newest files kept per owner
_SNAPSHOT_MAX_BYTES = 1_000_000
_SNAPSHOT_PAGE_CHARS = 240_000  # page-side outerHTML cap before transport

_FILL_FORM_HELPERS_JS = r"""
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
  const preview = (s, n) => { const t = String(s == null ? '' : s); return t.length > n ? t.slice(0, n) + '…' : t; };
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const SETTERS = {
    INPUT: Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set,
    TEXTAREA: Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set,
  };
  function setValue(el, v) {
    (SETTERS[el.tagName] || SETTERS.INPUT).call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  function pressClick(el) {
    el.scrollIntoView({block: 'center'});
    for (const t of ['mousedown', 'mouseup', 'click']) {
      el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
    }
  }
  const FOCUSABLE = 'input:not([type=hidden]):not([type=file]), textarea, select, [role=combobox], button';
  function containerOf(el) {
    let node = el;
    for (let i = 0; i < 6 && node && node !== document.body; i += 1) {
      if (node.querySelector && node.querySelector(FOCUSABLE)) return node;
      node = node.parentElement;
    }
    return el.parentElement || el;
  }
  function findContainer(query) {
    const q = norm(query);
    if (!q) return null;
    let best = null;
    for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,label,legend,[class*=title],[class*=Title],[class*=label],[class*=Label]')) {
      const t = norm(el.textContent).replace(/[\s*]+$/, '');
      if (!t) continue;
      let score = 0;
      if (t === q) score = 4;
      else if (t.startsWith(q)) score = 3;
      else if (q.startsWith(t) && t.length >= 4) score = 2;
      else if (t.includes(q) && q.length >= 4) score = 1;
      if (!score) continue;
      const c = containerOf(el);
      if (!c) continue;
      if (!best || score > best.score) best = {score, container: c, label: t};
    }
    return best;
  }
  function findInputByAttr(query) {
    const q = norm(query);
    if (!q) return null;
    for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=file]), textarea')) {
      if (!visible(el)) continue;
      const attrs = [el.placeholder, el.getAttribute('aria-label'), el.name, el.id];
      if (attrs.some((a) => a && norm(a).includes(q))) return el;
    }
    return null;
  }
  function resolveField(f) {
    if (f.selector) {
      const el = document.querySelector(f.selector);
      if (el) {
        return {el, container: el.closest('form, [class*=fieldContainer], [class*=FieldContainer], .form-group') || el.parentElement};
      }
      return null;
    }
    if (f.kind === 'checkbox') {
      const hit = findContainer(f.text || f.value);
      if (hit) {
        const boxes = [...hit.container.querySelectorAll('input[type=checkbox]')].filter(visible);
        if (boxes.length === 1) return {el: boxes[0], container: hit.container};
      }
      const q = norm(f.text || f.value);
      let best = null;
      for (const b of document.querySelectorAll('input[type=checkbox]')) {
        if (!visible(b)) continue;
        const wrap = b.closest('label') || b.parentElement;
        const t = norm(wrap ? wrap.textContent : '');
        if (q && t.includes(q)) { best = b; if (t.startsWith(q)) break; }
      }
      return best ? {el: best, container: best.closest('.checkbox, label, div') || best.parentElement} : null;
    }
    if (f.kind === 'click') {
      const q = norm(f.text || f.value);
      let best = null;
      for (const el of document.querySelectorAll('button, a, [role=button], input[type=submit], [class*=btn]')) {
        if (!visible(el)) continue;
        const t = norm(el.textContent || el.value);
        if (!t) continue;
        if (t === q) return {el, container: el.parentElement};
        if (q && t.includes(q) && !best) best = el;
      }
      return best ? {el: best, container: best.parentElement} : null;
    }
    const hit = findContainer(f.text);
    if (hit) {
      if (f.kind === 'select' || f.kind === 'combobox') {
        const combo = hit.container.querySelector('input[role=combobox], [class*=dropdown-select] input, select');
        if (combo) return {el: combo, container: hit.container};
      }
      const el = hit.container.querySelector('textarea, input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio])');
      if (el) return {el, container: hit.container};
    }
    const byAttr = findInputByAttr(f.text);
    if (byAttr) return {el: byAttr, container: byAttr.parentElement};
    return null;
  }
  // File inputs are usually visually hidden behind a "Choose file" button —
  // no visibility filter, and the attribute fallback covers aria-labeled
  // widgets (e.g. OpenReview's aria-label="pdf").
  function resolveFileInput(f) {
    if (f.selector) {
      const el = document.querySelector(f.selector);
      return (el && el.type === 'file') ? {el, container: el.parentElement} : null;
    }
    const hit = findContainer(f.text);
    if (hit) {
      const el = hit.container.querySelector('input[type=file]');
      if (el) return {el, container: hit.container};
    }
    const q = norm(f.text);
    if (q) {
      for (const el of document.querySelectorAll('input[type=file]')) {
        const attrs = [el.getAttribute('aria-label'), el.name, el.id, el.accept];
        if (attrs.some((a) => a && norm(a).includes(q))) return {el, container: el.parentElement};
      }
    }
    return null;
  }
  function fillNativeSelect(sel, value) {
    const q = norm(value);
    let best = null;
    for (const o of sel.options) {
      const t = norm(o.textContent); const v = norm(o.value);
      if (t === q || v === q) { best = o; break; }
      if ((t.includes(q) || v.includes(q)) && !best) best = o;
    }
    if (!best) {
      return {ok: false, error: `no option matching '${preview(value, 40)}'`,
              options: [...sel.options].map((o) => preview(o.textContent, 40)).filter(Boolean).slice(0, 12)};
    }
    sel.value = best.value;
    sel.dispatchEvent(new Event('input', {bubbles: true}));
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, matched: preview(best.textContent, 80), verified: sel.value === best.value};
  }
  const OPTION_SEL = '[role=option], [id*="-option-"], [class*="__option"], [class*="select-item-option"]';
  async function waitOptions(maxMs) {
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
      const opts = [...document.querySelectorAll(OPTION_SEL)]
        .filter((o) => visible(o) && norm(o.textContent) && norm(o.textContent).length <= 200);
      if (opts.length) return opts;
      await sleep(120);
    }
    return [];
  }
  async function fillCombobox(el, container, value) {
    if (el.tagName === 'SELECT') return fillNativeSelect(el, value);
    el.scrollIntoView({block: 'center'});
    el.focus();
    const control = el.closest('[class*=control]') || el;
    pressClick(control);
    el.focus();
    setValue(el, value);
    let opts = await waitOptions(2400);
    if (!opts.length) { setValue(el, ''); pressClick(control); opts = await waitOptions(1600); }
    const q = norm(value);
    let best = null; let bestScore = 0;
    for (const o of opts) {
      const t = norm(o.textContent);
      let s = 0;
      if (t === q) s = 4;
      else if (t.startsWith(q)) s = 3;
      else if (q.startsWith(t) && t.length >= 3) s = 2;
      else if (t.includes(q)) s = 1;
      if (s > bestScore) { best = o; bestScore = s; }
    }
    if (!best) {
      return {ok: false,
              error: opts.length ? `no option matching '${preview(value, 40)}'` : 'option menu did not open',
              options: opts.map((o) => preview(o.textContent, 40)).slice(0, 12)};
    }
    const matched = preview(best.textContent, 80);
    pressClick(best);
    await sleep(200);
    const scope = container || document;
    const pill = scope.querySelector('[class*=single-value], [class*=multi-value__label], [class*=selection-item]');
    const placeholder = scope.querySelector('[class*=placeholder]');
    const pillText = norm(pill ? pill.textContent : '');
    const verified = Boolean(
      (pillText && (pillText.includes(q.slice(0, 20)) || q.includes(pillText))) ||
      (!placeholder && norm(el.value) === ''));
    return {ok: true, matched, verified, actual: pillText ? preview(pill.textContent, 30) : ''};
  }
  async function fillCheckbox(el, value) {
    const want = !/^(false|0|no|off|uncheck)$/.test(norm(value));
    if (el.checked !== want) { pressClick(el); await sleep(80); }
    return {ok: true, verified: el.checked === want, actual: el.checked ? 'checked' : 'unchecked'};
  }
  async function fillText(el, value) {
    el.scrollIntoView({block: 'center'});
    el.focus();
    setValue(el, '');
    setValue(el, value);
    const verified = el.value === value;
    const isSecret = el.getAttribute && el.getAttribute('type') === 'password';
    return {ok: true, verified, actual: isSecret ? '[password]' : preview(el.value, 30)};
  }
  async function fillClick(el) { pressClick(el); await sleep(120); return {ok: true, verified: null}; }

  // ── search-then-pick widgets (OpenReview Authors, assignee/tag pickers) ──
  // Generic algorithm, no site selectors: type one exact query → submit the
  // search → match the result exactly (href param > text) → click the row's
  // add control (plus icon, else the row's sole non-search button) → wait
  // for the selection to materialize. Per-item stage errors name the FIRST
  // failing stage so repairs stay local; already-selected items are skipped.
  function widgetInput(container) {
    return container.querySelector(
      'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]), textarea');
  }
  function searchButton(container, input) {
    const btns = [...container.querySelectorAll('button, [role=button], input[type=submit]')].filter(visible);
    const named = btns.find((x) => /^(search|搜索|查找|find)$/i.test(norm(x.textContent || x.value)));
    if (named) return named;
    const scope = (input && input.closest('form')) || container;
    return scope.querySelector('button[type=submit]');
  }
  function linkMatches(a, query) {
    const href = a.href || '';
    if (href.includes(query)) return true;
    try {
      const u = new URL(href, location.href);
      for (const v of u.searchParams.values()) if (v === query) return true;
    } catch (e) { /* not a URL */ }
    return false;
  }
  function selectedHit(container, query) {
    const q = norm(query);
    for (const zone of container.querySelectorAll('[class*=selected], [class*=Selected], [class*=card], [class*=Card], [class*=pill], [class*=Pill], [class*=tag], [class*=Tag]')) {
      if (q && norm(zone.textContent).includes(q)) return zone;
      for (const a of zone.querySelectorAll('a[href]')) if (linkMatches(a, query)) return zone;
    }
    return null;
  }
  function findResultMatch(container, query, knownHrefs) {
    const q = norm(query);
    for (const a of container.querySelectorAll('a[href]')) {
      if (knownHrefs.has(a.href)) continue;
      if (linkMatches(a, query)) return a;
    }
    if (q.length >= 2) {
      for (const el of container.querySelectorAll('[role=option], [class*=result], [class*=Result], [class*=suggest], [class*=Suggest], li, tr')) {
        if (!visible(el)) continue;
        const t = norm(el.textContent);
        if (!t || t.length > 300) continue;
        if (t === q || t.startsWith(q) || (q.length >= 4 && t.includes(q))) return el;
      }
    }
    return null;
  }
  function findAddControl(match) {
    let node = match;
    for (let i = 0; i < 8 && node; i += 1, node = node.parentElement) {
      const icon = node.querySelector && node.querySelector(
        ".glyphicon-plus, .fa-plus, [class*='plus'], [aria-label*='add' i], [title*='add' i]");
      if (icon) {
        const btn = icon.closest("button, a, [role='button']");
        if (btn && visible(btn)) return btn;
      }
      const buttons = [...(node.querySelectorAll ? node.querySelectorAll('button, [role=button]') : [])]
        .filter(visible)
        .filter((b) => !/^(search|搜索|查找|find)$/i.test(norm(b.textContent || b.value)));
      if (buttons.length === 1 && /^(add|\+|添加)?$/i.test(norm(buttons[0].textContent || ''))) {
        return buttons[0];
      }
    }
    return null;
  }
  async function fillMultiSearch(f) {
    const items = String(f.value || '').split(/[,\n;]+/).map((s) => s.trim()).filter(Boolean).slice(0, 30);
    const outItems = [];
    let container = null;
    for (const item of items) {
      const rec = {value: item, ok: false, stage: 'resolve'};
      outItems.push(rec);
      if (Date.now() > DEADLINE) { rec.error = 'deadline exceeded'; rec.stage = 'deadline'; continue; }
      try {
        const hit = findContainer(f.text);
        container = (hit && hit.container) || container;
        if (!container) { rec.error = `no widget matches label "${preview(f.text, 40)}"`; continue; }
        if (selectedHit(container, item)) {
          rec.ok = true; rec.skipped = true; rec.stage = 'already-added';
          continue;
        }
        const input = widgetInput(container);
        if (!input) { rec.error = 'search input not found'; rec.stage = 'search-input'; continue; }
        const knownHrefs = new Set([...container.querySelectorAll('a[href]')].map((a) => a.href));
        input.scrollIntoView({block: 'center'});
        input.focus();
        setValue(input, '');
        await sleep(120);
        setValue(input, item);
        rec.stage = 'search';
        const btn = searchButton(container, input);
        let submittedSearch = false;
        if (btn) {
          const tWait = Date.now();
          while (btn.disabled && Date.now() - tWait < 1500) await sleep(120);
          if (!btn.disabled) { pressClick(btn); submittedSearch = true; }
        }
        if (!submittedSearch) {
          input.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Enter', keyCode: 13}));
          input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'Enter', keyCode: 13}));
        }
        rec.stage = 'result-match';
        const t0 = Date.now();
        let match = null;
        while (Date.now() - t0 < 8000) {
          match = findResultMatch(container, item, knownHrefs);
          if (!match && Date.now() - t0 > 4000) {
            // Some pickers render results in a body-level portal.
            match = findResultMatch(document.body, item, knownHrefs);
          }
          if (match) break;
          await sleep(200);
        }
        if (!match) { rec.error = `no result matched '${preview(item, 40)}'`; continue; }
        rec.stage = 'add-button';
        const addBtn = findAddControl(match);
        if (!addBtn) { rec.error = 'result found but its add (+) control was not'; continue; }
        pressClick(addBtn);
        rec.stage = 'confirm';
        const t1 = Date.now();
        let confirmed = false;
        while (Date.now() - t1 < 6000) {
          if (selectedHit(container, item) || !match.isConnected) { confirmed = true; break; }
          await sleep(200);
        }
        if (!confirmed) { rec.error = 'clicked add but the selection did not appear'; continue; }
        rec.ok = true; rec.stage = 'added';
        await sleep(300);
      } catch (e) {
        rec.error = 'exception: ' + (e && e.message ? e.message : String(e));
      }
    }
    const failed = outItems.filter((x) => !x.ok);
    const first = failed[0];
    return {
      ok: failed.length === 0,
      verified: failed.length === 0,
      items: outItems,
      error: first
        ? `${first.value}: ${first.error} (stage: ${first.stage})${failed.length > 1 ? ` +${failed.length - 1} more` : ''}`
        : '',
    };
  }
"""

_FILL_FORM_PAGE_SCRIPT = r"""
(async () => {
  'use strict';
  const PLAN = __FILL_FORM_PLAN__;
  const DEADLINE = Date.now() + PLAN.deadlineMs;
""" + _FILL_FORM_HELPERS_JS + r"""
  const out = {ok: true, results: [], submitted: false, deadlineExceeded: false,
               url: location.href, title: document.title, formHtml: ''};
  let firstContainer = null;
  for (const f of PLAN.fields) {
    const r = {index: f.index, kind: f.kind, ok: false, verified: null};
    if (Date.now() > DEADLINE) {
      out.deadlineExceeded = true;
      r.error = 'deadline exceeded before this field';
      out.results.push(r);
      continue;
    }
    try {
      if (f.kind === 'file') {
        // Server already streamed the file into this input via page_upload;
        // here we only verify the attachment landed (name must match).
        const foundFile = resolveFileInput(f);
        if (!foundFile) {
          r.error = `no file input matches ${f.selector ? 'selector ' + f.selector : 'label "' + preview(f.text, 40) + '"'}`;
        } else {
          if (!firstContainer) firstContainer = foundFile.container;
          const files = foundFile.el.files;
          if (files && files.length && files[0].name === f.expectFile) {
            r.ok = true; r.verified = true; r.actual = preview(files[0].name, 40);
          } else {
            r.error = files && files.length
              ? `file input holds '${preview(files[0].name, 40)}', expected '${preview(f.expectFile, 40)}'`
              : 'no file attached to the input';
          }
        }
        out.results.push(r);
        if (PLAN.fieldDelayMs) await sleep(PLAN.fieldDelayMs);
        continue;
      }
      const found = resolveField(f);
      if (!found) {
        r.error = `no field matches ${f.selector ? 'selector ' + f.selector : 'label "' + preview(f.text || f.value, 40) + '"'}`;
      } else {
        if (!firstContainer) firstContainer = found.container;
        let sub;
        if (f.kind === 'type' || f.kind === 'text') sub = await fillText(found.el, f.value);
        else if (f.kind === 'checkbox') sub = await fillCheckbox(found.el, f.value);
        else if (f.kind === 'select' || f.kind === 'combobox') sub = await fillCombobox(found.el, found.container, f.value);
        else if (f.kind === 'click') sub = await fillClick(found.el);
        else if (f.kind === 'multi_search') sub = await fillMultiSearch(f);
        else sub = {ok: false, error: `unknown field type '${f.kind}'`};
        if (sub.options) { r.available_options = sub.options; delete sub.options; }
        Object.assign(r, sub);
        if (sub.verified === false) {
          r.ok = false;
          r.error = r.error || `readback mismatch${sub.actual ? ' (page shows: ' + sub.actual + ')' : ''}`;
        }
      }
    } catch (e) {
      r.error = 'exception: ' + (e && e.message ? e.message : String(e));
    }
    out.results.push(r);
    if (PLAN.fieldDelayMs) await sleep(PLAN.fieldDelayMs);
  }
  out.ok = out.results.every((r) => r.ok) && !out.deadlineExceeded;
  if (out.ok && (PLAN.submitSelector || PLAN.submitText)) {
    let btn = PLAN.submitSelector ? document.querySelector(PLAN.submitSelector) : null;
    if (!btn && PLAN.submitText) {
      const q = norm(PLAN.submitText);
      for (const el of document.querySelectorAll('button, [role=button], input[type=submit]')) {
        if (visible(el) && norm(el.textContent || el.value).includes(q)) { btn = el; break; }
      }
    }
    if (btn) { pressClick(btn); await sleep(600); out.submitted = true; }
    else { out.ok = false; out.submitError = 'submit button not found'; }
  }
  try {
    const root = (firstContainer && firstContainer.closest('form, [class*=NoteEditor], [class*=note-editor], main')) ||
                 document.querySelector('form') || document.body;
    let html = (root && root.outerHTML) || '';
    out.snapshotTruncated = html.length > PLAN.snapshotChars;
    if (out.snapshotTruncated) html = html.slice(0, PLAN.snapshotChars);
    out.formHtml = html;
  } catch (e) { /* snapshot is advisory */ }
  return out;
})()
"""

#: Tag label-addressed file inputs with data-tofu-ref so the page_upload
#: bridge command (which needs a selector/ref) can find them. Shares the
#: resolver helpers with the fill script — one source of matching logic.
_TAG_FILE_INPUTS_SCRIPT = r"""
(async () => {
  'use strict';
  const PLAN = __FILL_FORM_PLAN__;
""" + _FILL_FORM_HELPERS_JS + r"""
  const out = {ok: true, results: []};
  for (const f of PLAN.fields) {
    const r = {index: f.index, ok: false};
    try {
      const found = resolveFileInput(f);
      if (!found) {
        r.error = `no file input matches label "${preview(f.text, 40)}"`;
      } else {
        found.el.setAttribute('data-tofu-ref', f.ref);
        r.ok = true;
      }
    } catch (e) {
      r.error = 'exception: ' + (e && e.message ? e.message : String(e));
    }
    out.results.push(r);
  }
  out.ok = out.results.every((r) => r.ok);
  return out;
})()
"""


#: The extension's page_upload accepts at most 28M base64 chars (~20.9 MB
#: decoded); keep a clean 20 MiB server-side ceiling with an explicit error.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _resolve_upload_path(value: str, project_path: str | None):
    """Resolve a model-named local file for browser upload.

    Relative paths (including multi-root ``name:path``) resolve through the
    same workspace seam as the file tools; absolute/~ paths are used as-is.
    Returns ``(abs_path, None)`` or ``(None, error)``.
    """
    raw = str(value or '').strip()
    if not raw:
        return None, 'empty file path'
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        path = os.path.realpath(expanded)
    else:
        if not project_path:
            return None, (f"'{raw}' is relative but no workspace is active; "
                          'pass an absolute path')
        from lib.project_mod.path_resolution import _resolve_base
        try:
            base, rel = _resolve_base(project_path, raw)
        except Exception as exc:
            logger.debug('resolve upload base failed: %s', exc)
            return None, f'cannot resolve path {raw!r}: {exc}'
        path = os.path.realpath(os.path.join(base, rel))
    if not os.path.isfile(path):
        return None, f'file not found: {raw}'
    return path, None


def _prepare_upload(field: dict, project_path: str | None):
    """Read one local file for a type='file' field.

    Returns a dict with ok/error and, on success, the page_upload payload
    (base64 data, filename, mimeType) plus the expected input filename.
    """
    path, err = _resolve_upload_path(field.get('value'), project_path)
    if err:
        return {'ok': False, 'error': err}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {'ok': False, 'error': f'cannot stat {path}: {exc}'}
    if size > _MAX_UPLOAD_BYTES:
        return {'ok': False,
                'error': (f"'{os.path.basename(path)}' is "
                          f'{size / 1048576:.1f} MB — over the 20 MB browser '
                          'upload limit; attach it manually or compress it')}
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as exc:
        return {'ok': False, 'error': f'cannot read {path}: {exc}'}
    filename = os.path.basename(path)
    mime, _guess = mimetypes.guess_type(path)
    return {'ok': True,
            'payload': {'data': base64.b64encode(data).decode('ascii'),
                        'filename': filename,
                        'mimeType': mime or 'application/octet-stream'},
            'expectFile': filename}


def _write_form_snapshot(owner_user_id, tab_id, html) -> str | None:
    """Persist filled-form HTML under a bounded per-owner snapshot dir.

    Transient reconstructible evidence: newest ``_SNAPSHOT_KEEP`` files per
    owner, hard byte ceiling, pruned oldest-first on write. The content is
    the owner's own page; it is never logged.
    """
    if not html:
        return None
    try:
        owner = re.sub(r'[^0-9A-Za-z_-]', '_', str(owner_user_id or 'unknown'))
        directory = os.path.join(_SNAPSHOT_DIR, f'owner-{owner}')
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        path = os.path.join(
            directory, f'fill-tab{int(tab_id)}-{stamp}-{uuid.uuid4().hex[:6]}.html')
        data = html.encode('utf-8', 'replace')[:_SNAPSHOT_MAX_BYTES]
        tmp_path = path + '.part'
        with open(tmp_path, 'wb') as fh:
            fh.write(data)
        os.replace(tmp_path, path)
        files = sorted(
            (os.path.join(directory, f) for f in os.listdir(directory)
             if f.endswith('.html')),
            key=os.path.getmtime)
        for old in files[:max(0, len(files) - _SNAPSHOT_KEEP)]:
            try:
                os.unlink(old)
            except OSError:
                pass
        return path
    except Exception as exc:  # snapshot is advisory — never fail the fill
        logger.debug('form snapshot write failed: %s', exc)
        return None


def _fill_failure(shape: str, total: int, **extra) -> dict[str, Any]:
    out = {'success': False, 'fields_filled': 0, 'fields_failed': total,
           'field_results': [], 'submitted': False, 'steps_completed': 0,
           'error': shape}
    out.update(extra)
    return out


def fill_form_single_pass(
    tab_id: int, fields: list[dict[str, str]],
    submit_selector: str | None = None,
    field_delay: float = 0.2, timeout: float = 45.0,
    submit_text: str | None = None,
    *,
    send,
    owner_user_id=None,
    project_path=None,
) -> dict[str, Any]:
    """Fill a whole form with ONE page-side pass and verify every field.

    Compiles ``fields`` into a single async script executed through one
    ``execute_js`` bridge command. The page resolves each field by CSS
    selector or visible label, fills it (text/checkbox/select/click), reads
    back the resulting DOM state, and only then clicks the submit control.

    ``type='file'`` fields attach a local file to an ``input[type=file]``:
    the server resolves the path through the workspace seam, streams the
    bytes through the extension's ``page_upload`` (tagging label-addressed
    inputs with ``data-tofu-ref`` first), and the fill script verifies the
    attachment by filename — so the submit gate covers uploads too.

    Returns the same result envelope the sequential relay used, plus
    ``verified`` flags, a repair-oriented error string, and a bounded
    per-owner ``snapshot_path`` with the filled form's HTML.
    """
    start_time = time.time()
    try:
        fields = list(fields or [])
        if not fields:
            return _fill_failure('no fields given', 0)
        plan_fields = []
        for i, field in enumerate(fields):
            field = field if isinstance(field, dict) else {}
            plan_fields.append({
                'index': i,
                'kind': str(field.get('type', 'type') or 'type').strip().lower(),
                'selector': str(field.get('selector') or ''),
                'text': str(field.get('text') or ''),
                'value': '' if field.get('value') is None else str(field.get('value')),
            })

        # ── type='file' fields: read local bytes, tag label-addressed file
        # inputs, then stream each file through page_upload. Failures here
        # fail only their own field; the submit gate still protects the rest.
        pre_failures: dict[int, str] = {}
        file_fields = [f for f in plan_fields if f['kind'] == 'file']
        uploads: dict[int, dict] = {}
        if file_fields:
            for f in file_fields:
                prepared = _prepare_upload(f, project_path)
                if not prepared['ok']:
                    pre_failures[f['index']] = prepared['error']
                else:
                    uploads[f['index']] = prepared
                    f['expectFile'] = prepared['expectFile']
            tag_targets = [f for f in file_fields
                           if f['index'] in uploads and not f['selector']]
            if tag_targets:
                tag_plan = {'fields': [
                    {'index': f['index'], 'text': f['text'],
                     'ref': f"tofu-upload-{f['index']}"}
                    for f in tag_targets]}
                tag_code = _TAG_FILE_INPUTS_SCRIPT.replace(
                    '__FILL_FORM_PLAN__', json.dumps(tag_plan, ensure_ascii=False))
                tag_page, tag_error = send('execute_js', {
                    'tabId': int(tab_id), 'code': tag_code,
                }, timeout=20)
                tagged: set[int] = set()
                if tag_error:
                    for f in tag_targets:
                        pre_failures[f['index']] = (
                            f'could not locate the file input: {tag_error}')
                elif isinstance(tag_page, dict) and tag_page.get('__error'):
                    for f in tag_targets:
                        pre_failures[f['index']] = (
                            f"could not locate the file input: "
                            f"{tag_page.get('message', 'unknown')}")
                elif isinstance(tag_page, dict):
                    for r in tag_page.get('results') or []:
                        if r.get('ok'):
                            tagged.add(r.get('index'))
                        else:
                            pre_failures[r.get('index')] = r.get(
                                'error', 'file input not found')
                for f in tag_targets:
                    if f['index'] not in tagged and f['index'] not in pre_failures:
                        pre_failures[f['index']] = 'file input not tagged'
            for f in file_fields:
                if f['index'] in pre_failures or f['index'] not in uploads:
                    continue
                params = {'tabId': int(tab_id), **uploads[f['index']]['payload']}
                if f['selector']:
                    params['selector'] = f['selector']
                else:
                    params['ref'] = f"tofu-upload-{f['index']}"
                _res, up_error = send('page_upload', params, timeout=60)
                if up_error:
                    pre_failures[f['index']] = f'upload failed: {up_error}'

        # The fill script verifies successful uploads by filename; failed
        # ones are excluded (their fields already failed pre-flight).
        script_fields = [f for f in plan_fields
                         if f['kind'] != 'file' or f['index'] not in pre_failures]
        deadline_ms = max(10_000, min(90_000, int(float(timeout) * 1000)))
        plan = {
            'fields': script_fields,
            'fieldDelayMs': max(0, int(float(field_delay) * 1000)),
            # A pre-flight upload failure must never let the script submit:
            # the page-side gate only sees the fields it was given.
            'submitSelector': str(submit_selector or '') if not pre_failures else '',
            'submitText': str(submit_text or '') if not pre_failures else '',
            'deadlineMs': deadline_ms,
            'snapshotChars': _SNAPSHOT_PAGE_CHARS,
        }
        code = _FILL_FORM_PAGE_SCRIPT.replace(
            '__FILL_FORM_PLAN__', json.dumps(plan, ensure_ascii=False))
        send_timeout = max(20, min(100, deadline_ms // 1000 + 15))
        page, error = send('execute_js', {
            'tabId': int(tab_id), 'code': code,
        }, timeout=send_timeout)
        if error:
            return _fill_failure(f'form fill command failed: {error}', len(fields))
        if isinstance(page, dict) and page.get('__error'):
            return _fill_failure(
                f"page script error: {page.get('message', 'unknown')}", len(fields))
        if not isinstance(page, dict) or not isinstance(page.get('results'), list):
            return _fill_failure('page returned no field results', len(fields))

        field_results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for r in page['results']:
            r = r if isinstance(r, dict) else {'ok': False, 'error': 'malformed result'}
            entry = {
                'index': r.get('index'),
                'type': r.get('kind'),
                'ok': bool(r.get('ok')),
                'verified': r.get('verified'),
            }
            plan_field = plan_fields[r['index']] \
                if isinstance(r.get('index'), int) and 0 <= r['index'] < len(plan_fields) \
                else {}
            if plan_field.get('selector'):
                entry['selector'] = plan_field['selector']
            if plan_field.get('text'):
                entry['text'] = plan_field['text']
            for key in ('matched', 'actual', 'error', 'available_options',
                        'items'):
                if r.get(key):
                    entry[key] = r[key]
            field_results.append(entry)
            if isinstance(r.get('index'), int):
                seen.add(r['index'])
        for f in plan_fields:
            if f['index'] in pre_failures and f['index'] not in seen:
                entry = {'index': f['index'], 'type': f['kind'], 'ok': False,
                         'verified': False, 'error': pre_failures[f['index']]}
                if f.get('selector'):
                    entry['selector'] = f['selector']
                if f.get('text'):
                    entry['text'] = f['text']
                field_results.append(entry)
        field_results.sort(key=lambda r: (r['index'] is None, r['index']))

        fields_filled = sum(1 for r in field_results if r['ok'])
        fields_failed = len(fields) - fields_filled
        all_ok = fields_failed == 0 and not page.get('deadlineExceeded')
        submit_error = page.get('submitError')
        submitted = bool(page.get('submitted')) and all_ok and not submit_error

        snapshot_path = _write_form_snapshot(
            owner_user_id, tab_id, page.get('formHtml') or '')

        result_out = {
            'success': bool(all_ok and not submit_error),
            'fields_filled': fields_filled,
            'fields_failed': fields_failed,
            'field_results': field_results,
            'submitted': submitted,
            'steps_completed': fields_filled,
            'elapsed_ms': round((time.time() - start_time) * 1000, 2),
        }
        if snapshot_path:
            result_out['snapshot_path'] = snapshot_path
        if all_ok and not submit_error:
            result_out['details'] = {
                'verified': f'{fields_filled}/{len(fields)} fields verified',
                'submitted': submitted,
            }
            if snapshot_path:
                result_out['details']['snapshot'] = snapshot_path
            return result_out

        # Repair-oriented failure text: name the exact fields and observed
        # options so the model re-calls with ONLY the failed field(s).
        failures = [r for r in field_results if not r['ok']]
        lines = []
        for r in failures[:3]:
            label = r.get('text') or r.get('selector') or f"#{r.get('index', 0) + 1}"
            line = f"{label}: {r.get('error', 'failed')}"
            if r.get('available_options'):
                line += ' (options: ' + ', '.join(
                    str(o) for o in r['available_options'][:8]) + ')'
            lines.append(line)
        more = f' +{len(failures) - 3} more' if len(failures) > 3 else ''
        parts = [f'{fields_failed} of {len(fields)} field(s) failed '
                 f'({"; ".join(lines)}{more}). '
                 'Repair by re-calling browser_fill_form with ONLY the failed '
                 'field(s); filled fields are replaced cleanly.']
        if submit_selector or submit_text:
            parts.append('Submit skipped.')
        if submit_error:
            parts.append(f'Submit failed: {submit_error}.')
        if snapshot_path:
            parts.append(f'Page snapshot: {snapshot_path}')
        result_out['error'] = ' '.join(parts)
        return result_out
    except Exception as exc:
        logger.warning('fill_form_single_pass failed: %s', exc, exc_info=True)
        return _fill_failure(f'Exception: {exc}', len(fields or []),
                             elapsed_ms=round((time.time() - start_time) * 1000, 2))


# ═══════════════════════════════════════════════════════
#  Tool Definitions (for LLM function calling)
# ═══════════════════════════════════════════════════════

ADVANCED_BROWSER_TOOL_MENU_CLICK = {
    "type": "function",
    "function": {
        "name": "browser_menu_click",
        "description": (
            "Open a menu and click an item, in ONE call. via='hover' (default) "
            "opens dropdown/nav menus; via='right_click' opens context menus. "
            "Name the target by target_text (fuzzy-matched, e.g. 'File') or "
            "target_selector; the item is clicked by visible-text match "
            "(item_text, case-insensitive substring). submenu_text handles "
            "nested menus. If the item is not found, the available menu items "
            "are returned so you can retry with the right text. If the item "
            "opens a new tab, the result says so and that tab becomes the "
            "working tab."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Tab ID (omit to use the current working tab)"},
                "target_text": {"type": "string", "description": "Visible text of the element that opens the menu (preferred over target_selector)"},
                "target_selector": {"type": "string", "description": "CSS selector of the element that opens the menu"},
                "item_text": {"type": "string", "description": "Text of the menu item to click (case-insensitive substring match)"},
                "via": {"type": "string", "enum": ["hover", "right_click"], "description": "How the menu opens (default: hover)"},
                "submenu_text": {"type": "string", "description": "(Optional) Text of a nested submenu item to click after item_text"},
                "menu_wait": {"type": "number", "description": "Seconds to wait for the menu to appear (default: 0.5)"},
                "timeout": {"type": "number", "description": "Total timeout in seconds (default: 5.0)"},
            },
            "required": ["item_text"]
        }
    }
}

ADVANCED_BROWSER_TOOL_FILL_FORM = {
    "type": "function",
    "function": {
        "name": "browser_fill_form",
        "description": (
            "Fill MULTIPLE form fields in ONE call (one page-side pass), "
            "verify each by read-back, optionally submit. PREFER THIS "
            "for 2+ fields — never loop per-field tools.\n"
            "Target fields by visible LABEL (text=, fuzzy) or selector=. "
            "types: 'type' (text/textarea; CLEARED first — replaces, never "
            "appends), 'checkbox' (label text; value true/false), 'select' "
            "(combobox/native; value = option text; options listed if "
            "unmatched), 'click' (button by visible text), 'file' (attach "
            "local file; value = workspace/absolute path, ≤20 MB), "
            "'multi_search' (search-then-pick widget, e.g. OpenReview "
            "Authors: value = comma-separated exact queries — IDs beat "
            "names; added via the row's +; already-added skipped; stage "
            "errors per item).\n"
            "Submit fires ONLY when all fields verify — never post a "
            "half-filled form. Failures name the exact field(s): repair "
            "with ONLY the failed one(s). Filled-form HTML lands in a "
            "snapshot file (path returned) — grep it, don't re-read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Tab ID (omit for working tab)"},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector of the field"},
                            "text": {"type": "string", "description": "Visible field label (fuzzy; preferred over selector)"},
                            "value": {"type": "string", "description": "Value (true/false for checkbox; option text for select; path for file)"},
                            "type": {"type": "string", "enum": ["type", "checkbox", "select", "click", "file", "multi_search"], "description": "Kind (default: type)"}
                        },
                        "required": ["value"]
                    },
                    "description": "List of fields to fill"
                },
                "submit_selector": {"type": "string", "description": "CSS selector of submit button"},
                "submit_text": {"type": "string", "description": "Submit button text"},
                "field_delay": {"type": "number", "description": "Delay between fields (s, default 0.2)"},
            },
            "required": ["fields"]
        }
    }
}

ADVANCED_BROWSER_TOOLS = [
    ADVANCED_BROWSER_TOOL_MENU_CLICK,
    ADVANCED_BROWSER_TOOL_FILL_FORM,
]

ADVANCED_BROWSER_TOOL_NAMES = {
    'browser_menu_click',
    'browser_fill_form',
}
