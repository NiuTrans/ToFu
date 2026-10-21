"""Read a bounded rendered DOM through the ordinary source-extraction owner.

Entries: wait_for_rendered_source and extract_rendered_source. The
browser pool owns navigation, identity and context retirement. This
module only projects received DOM content, including open shadow/slot content,
and delegates Markdown/source fidelity to html_extract via the existing worker.
"""

import json
import time

from tofu_search.config import get_config
from tofu_search.fetch._async_browser_runtime import run_browser_blocking
from tofu_search.fetch.html_extract import extract_html_text
from tofu_search.fetch.html_rendering import PRIMARY_SELECTORS
from tofu_search.fetch.utils import _looks_like_spa_shell
from tofu_search.log import get_logger

logger = get_logger(__name__)

# Empty primary content must not be masked by a long navigation menu. Short
# readable primary regions need no arbitrary 200-character readiness floor.
RENDERED_TEXT_LENGTH = """(() => {
    if (!document.body) return 0;
    const shown = element => {
        for (let p = element; p; p = p.parentElement) {
            if (p.hidden || getComputedStyle(p).display === 'none') return false;
        }
        return true;
    };
    let regions = [];
    for (const selector of __PRIMARY_SELECTORS__) {
        regions = [...document.querySelectorAll(selector)].filter(shown);
        if (regions.length) break;
    }
    return (regions.length ? regions : [document.body]).filter(shown)
        .map(element => element.innerText || '').join('\\n').trim().length;
})()""".replace('__PRIMARY_SELECTORS__', json.dumps(PRIMARY_SELECTORS))
RENDERED_READY = RENDERED_TEXT_LENGTH + ' > 0'


async def wait_for_rendered_source(page, *, deadline):
    """Wait for primary text and bounded stability within the existing deadline.

    DOM readiness is heuristic, not proof all future page updates have ended.
    No minimum-text floor, fresh timeout after navigation, or background poll.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    try:
        await page.wait_for_function(RENDERED_READY, timeout=min(remaining, 12) * 1000)
    except Exception:
        logger.debug('Primary content readiness did not complete', exc_info=True)
    previous, stable = 0, 0
    for _ in range(8):
        if time.monotonic() >= deadline:
            break
        try:
            length = await page.evaluate(RENDERED_TEXT_LENGTH)
        except Exception:
            break
        if length == previous and length > 0:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = length
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await page.wait_for_timeout(min(remaining * 1000, 500))

_RENDERED_HTML = """({maxSourceChars}) => {
    if (!document.body) return {html: '', truncated: false};
    // An inactive document does not load copied resources or execute handlers.
    // Source DOM, attributes, event handlers and page state are never mutated.
    const copy = document.implementation.createHTMLDocument('');
    const body = copy.importNode(document.body, false);
    copy.body.replaceWith(body);
    const excluded = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME']);
    const stack = [{source: document.body, parent: copy.documentElement, root: true, textVisible: true}];
    let remaining = maxSourceChars - body.outerHTML.length, truncated = false;
    if (remaining < 0) return {html: '', truncated: true};
    while (stack.length && !truncated) {
        const {source, parent, root, textVisible} = stack.pop();
        if (source.nodeType === Node.TEXT_NODE) {
            if (!textVisible) continue;
            parent.appendChild(copy.createTextNode(source.data.slice(0, remaining)));
            if (source.data.length > remaining) truncated = true;
            remaining -= Math.min(source.data.length, remaining);
            continue;
        }
        if (source.nodeType !== Node.ELEMENT_NODE || excluded.has(source.tagName)) continue;
        const style = getComputedStyle(source);
        if (source.hidden || style.display === 'none' || style.contentVisibility === 'hidden') continue;
        const target = root ? copy.body : copy.importNode(source, false);
        if (!root) {
            const size = target.outerHTML.length;
            if (size > remaining) { truncated = true; break; }
            remaining -= size;
            parent.appendChild(target);
        }
        // visibility:hidden can have explicitly visible descendants. Omit only
        // its own text; descendants retain their computed visibility decisions.
        const childrenVisible = style.visibility !== 'hidden' && style.visibility !== 'collapse';
        let children;
        if (source.tagName === 'SLOT') {
            const assigned = source.assignedNodes({flatten: true});
            children = assigned.length ? assigned : source.childNodes;
        } else {
            children = source.shadowRoot ? source.shadowRoot.childNodes : source.childNodes;
        }
        // Depth-first source order also makes a bounded snapshot a true prefix.
        for (let i = children.length - 1; i >= 0; i--) {
            stack.push({source: children[i], parent: target, root: false, textVisible: childrenVisible});
        }
    }
    return {html: copy.documentElement.outerHTML, truncated};
}"""


def _extract_snapshot(html, max_chars, url):
    scope = {}
    text = extract_html_text(html, max_chars or 0, url=url, scope=scope)
    if _looks_like_spa_shell(html, scope.get('document_text', text),
                            signals=scope.get('rendering')):
        return None
    return text


async def extract_rendered_source(page, max_chars, url, *, document=None):
    """Extract the visible source snapshot without falling back to raw DOM text.

    Snapshot limits derive from the existing response budget. Partial snapshots
    retain a truncation marker; an unavailable projection is not evidence.
    Exceptions propagate to the pool's existing fallback/retirement boundary.
    """
    if document is not None and not document.allows_source:
        return None
    generation = document.generation if document is not None else None
    snapshot = await page.evaluate(_RENDERED_HTML, {
        'maxSourceChars': max(1, int(get_config().fetch_max_bytes)),
    })
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('html'), str):
        return None
    text = await run_browser_blocking(_extract_snapshot, snapshot['html'], max_chars, url)
    if document is not None and (not document.allows_source or document.generation != generation):
        # Navigation overlapped DOM projection/extraction. Do not return an
        # older snapshot after another document request or response arrived.
        return None
    if text and snapshot.get('truncated') and not text.endswith('\n[…truncated]'):
        text += '\n[…truncated]'
    return text or None
