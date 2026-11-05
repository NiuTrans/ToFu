"""Page media inventory: a fixed DOM projection, its acceptance and rendering.

Responsibility: let an agent discover the listed <img>/<video>/<audio>
elements of ANY page in its already-resolved, policy-checked working tab and
acquire them by URL or element, without site adapters and without credential-
or signature-bearing media URLs entering model context.

Entry points:
  media_inventory_script()  the fixed, server-authored, read-only page script
                            that browser_read_page(mode="media") runs through
                            the extension's existing execute_js command.
  accept_media_inventory()  untrusted script result -> bounded, admitted
                            inventory (URLs via content.observed_url, page
                            text via content.clean_text).
  render_media_inventory()  compact model text, one line per distinct
                            resource, whose footer routes acquisition through
                            browser_download_url_to_server(url= or selector=).
  MEDIA_ELEMENT_JS          the in-page definitions of "rendered", "displayed
                            resource" and "listed", also embedded by
                            download_target._link_reader_script.

Dependencies: lib.browser.content (observed_url, clean_text) and
lib.browser.content_contract (the receipt record bound). The read handler
(handlers/_page.py) owns transport and re-checks the reported page URL with
handlers/_tabs._result_url_allowed before acceptance.
"""

from __future__ import annotations

import json
import math

from .content import clean_text, observed_url
from .content_contract import CONTENT_RECEIPT_SCHEMA

_RECORD_SCHEMA = CONTENT_RECEIPT_SCHEMA['properties']['records']
#: One inventory item is one receipt record, so the inventory shares the
#: receipt's record bound instead of inventing its own.
MEDIA_INVENTORY_MAX_ITEMS = _RECORD_SCHEMA['maxItems']
_TEXT_MAX_CHARS = _RECORD_SCHEMA['items']['properties']['title']['maxLength']
_KINDS = frozenset({'image', 'video', 'audio'})
_UNFETCHABLE_SOURCES = frozenset({'blob', 'data', 'stream', 'none'})

# Read-only DOM definitions shared by both page scripts. A resource counts as
# displayed only through the browser's own resolution (currentSrc, reflected
# src, first <source src>, srcObject); data: payloads and blob:/stream handles
# never leave the page because the server could not fetch them anyway (`raw`
# only compares resources inside the page). An element is LISTED when it is a
# rendered image, or a video/audio that is rendered or has a displayed
# resource: players hide the playing element behind their own chrome and
# <audio> without controls is never rendered.
MEDIA_ELEMENT_JS = """
      const renderedRect = (element) => {
        const rect = element.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) return null;
        const style = getComputedStyle(element);
        return style.display === 'none' || style.visibility === 'hidden'
          || style.visibility === 'collapse' ? null : rect;
      };
      const mediaKind = (element) => element instanceof HTMLImageElement ? 'image'
        : element instanceof HTMLVideoElement ? 'video'
        : element instanceof HTMLAudioElement ? 'audio' : '';
      const reflectedSrc = (element) =>
        (element.getAttribute('src') || '').trim() ? element.src : '';
      const displayedResource = (element) => {
        if (element.srcObject) return {source: 'stream', url: '', raw: ''};
        let raw = element.currentSrc || reflectedSrc(element);
        if (!raw && element instanceof HTMLMediaElement) {
          const first = Array.from(element.children).find(
            (child) => child.localName === 'source' && reflectedSrc(child));
          raw = first ? first.src : '';
        }
        const scheme = ((/^([a-z][a-z0-9+.-]*):/i.exec(raw) || [])[1] || '').toLowerCase();
        if (scheme === 'http' || scheme === 'https') return {source: 'http', url: raw, raw};
        return scheme === 'blob' || scheme === 'data'
          ? {source: scheme, url: '', raw} : {source: 'none', url: '', raw: ''};
      };
      const listedMedia = (element) => {
        const kind = mediaKind(element);
        if (!kind) return null;
        const rect = renderedRect(element);
        const resource = displayedResource(element);
        if (!rect && (kind === 'image' || resource.source === 'none')) return null;
        return {kind, rect, resource};
      };"""


def media_inventory_script() -> str:
    """Return the fixed read-only inventory projection for one page."""
    bounds = json.dumps({'maxItems': MEDIA_INVENTORY_MAX_ITEMS,
                         'maxText': _TEXT_MAX_CHARS})
    return f"""(() => {{
      const bounds = {bounds};{MEDIA_ELEMENT_JS}
      const uniqueMatch = (selector, element) => {{
        try {{
          const hits = document.querySelectorAll(selector);
          return hits.length === 1 && hits[0] === element;
        }} catch (_error) {{
          return false;
        }}
      }};
      const stepOf = (node) => {{
        let index = 1;
        let alone = true;
        for (let sibling = node.previousElementSibling; sibling;
             sibling = sibling.previousElementSibling) {{
          if (sibling.localName === node.localName) {{
            index += 1;
            alone = false;
          }}
        }}
        for (let sibling = node.nextElementSibling; alone && sibling;
             sibling = sibling.nextElementSibling) {{
          if (sibling.localName === node.localName) alone = false;
        }}
        const tag = CSS.escape(node.localName);
        return alone ? tag : tag + ':nth-of-type(' + index + ')';
      }};
      // Bottom-up: stop at the shortest suffix already unique in the document
      // or at the nearest uniquely identified #id ancestor, else at :root.
      const selectorOf = (element) => {{
        if (element.getRootNode() !== document) return '';
        const steps = [];
        for (let node = element; node; node = node.parentElement) {{
          const anchor = node.id ? '#' + CSS.escape(node.id) : '';
          if (anchor && uniqueMatch(anchor, node)) {{
            steps.unshift(anchor);
            break;
          }}
          if (node === document.documentElement) {{
            steps.unshift(':root');
            break;
          }}
          steps.unshift(stepOf(node));
          if (uniqueMatch(steps.join(' > '), element)) break;
        }}
        const selector = steps.join(' > ');
        return uniqueMatch(selector, element) ? selector : '';
      }};
      const found = [];
      const visit = (root) => {{
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {{
          if (mediaKind(node)) found.push(node);
          if (node.shadowRoot) visit(node.shadowRoot);
        }}
      }};
      visit(document);
      const listed = [];
      found.forEach((element, order) => {{
        const media = listedMedia(element);
        if (media) listed.push({{element, order, ...media}});
      }});
      const area = (entry) => entry.rect ? entry.rect.width * entry.rect.height : 0;
      listed.sort((a, b) => (a.kind === 'image') - (b.kind === 'image')
        || area(b) - area(a) || a.order - b.order);
      // One entry per kind and raw resource string: the first after ordering,
      // with its occurrence count. Stream and source-less media have no
      // resource string and never collapse.
      const distinct = [];
      const firstByResource = new Map();
      for (const entry of listed) {{
        const key = entry.resource.raw && entry.kind + ' ' + entry.resource.raw;
        const first = key && firstByResource.get(key);
        if (first) {{
          first.count += 1;
          continue;
        }}
        entry.count = 1;
        distinct.push(entry);
        if (key) firstByResource.set(key, entry);
      }}
      const items = distinct.slice(0, bounds.maxItems).map(
        ({{element, order, kind, rect, resource, count}}) => {{
        const figure = element.closest('figure');
        const caption = figure && Array.from(figure.children).find(
          (child) => child.localName === 'figcaption');
        const label = [element.getAttribute('alt'), element.getAttribute('title'),
          element.getAttribute('aria-label'), caption && caption.textContent]
          .map((text) => (text || '').trim()).find(Boolean) || '';
        return {{
          order, kind, count, hidden: !rect, selector: selectorOf(element),
          width: rect ? rect.width : 0, height: rect ? rect.height : 0,
          intrinsic_width: kind === 'image' ? element.naturalWidth
            : kind === 'video' ? element.videoWidth : 0,
          intrinsic_height: kind === 'image' ? element.naturalHeight
            : kind === 'video' ? element.videoHeight : 0,
          duration: kind !== 'image' && Number.isFinite(element.duration)
            ? element.duration : null,
          label: label.slice(0, bounds.maxText),
          in_viewport: Boolean(rect) && rect.bottom > 0 && rect.right > 0
            && rect.top < innerHeight && rect.left < innerWidth,
          source: resource.source, url: resource.url,
          poster: kind === 'video' && /^https?:/i.test(element.poster) ? element.poster : ''
        }};
      }});
      return {{url: location.href, title: document.title, total: listed.length,
              distinct: distinct.length, items}};
    }})()"""


def _text(value) -> str:
    """Page text through the content owner's instruction filter and redaction."""
    if not isinstance(value, str):
        return ''
    return ' '.join(clean_text(value[:_TEXT_MAX_CHARS]).splitlines())


def _size(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _whole(value) -> int:
    """A positive page-reported integer, else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _accept_item(raw, index, page, owner_user_id):
    if not isinstance(raw, dict) or raw.get('kind') not in _KINDS:
        return None
    kind = raw['kind']
    source = raw.get('source')
    if source == 'http':
        url = observed_url(raw.get('url'), page, owner_user_id)
        reason = '' if url else 'withheld_signed'
    else:
        source = source if source in _UNFETCHABLE_SOURCES else 'none'
        url, reason = '', 'not_http'
    selector = raw.get('selector') if isinstance(raw.get('selector'), str) else ''
    # A selector is an opaque single-line argument for the download tool: show
    # it only when the content cleaner would leave it byte-identical.
    if '\n' in selector or clean_text(selector) != selector:
        selector = ''
    order = raw.get('order')
    duration = raw.get('duration')
    # Only timed media are listed without a rendered box.
    hidden = kind != 'image' and raw.get('hidden') is True
    return {
        'order': order if isinstance(order, int) and not isinstance(order, bool) else index,
        'kind': kind,
        'count': max(_whole(raw.get('count')), 1),
        'hidden': hidden,
        'selector': selector,
        'width': 0.0 if hidden else _size(raw.get('width')),
        'height': 0.0 if hidden else _size(raw.get('height')),
        'intrinsic_width': round(_size(raw.get('intrinsic_width'))),
        'intrinsic_height': round(_size(raw.get('intrinsic_height'))),
        'duration': (_size(duration) if kind != 'image' and _size(duration) else None),
        'label': _text(raw.get('label')),
        'in_viewport': not hidden and raw.get('in_viewport') is True,
        'source': source,
        'url': url,
        'reason': reason,
        'poster': (observed_url(raw.get('poster'), page, owner_user_id)
                   if kind == 'video' else ''),
    }


def accept_media_inventory(result: dict, *, owner_user_id: str) -> dict:
    """Admit one untrusted inventory whose page URL the caller policy-checked."""
    page = str(result.get('url') or '')
    raw_items = result.get('items')
    accepted = []
    for index, raw in enumerate(raw_items if isinstance(raw_items, list) else ()):
        item = _accept_item(raw, index, page, owner_user_id)
        if item is not None:
            accepted.append(item)
    accepted.sort(key=lambda item: (item['kind'] == 'image',
                                    -item['width'] * item['height'], item['order']))
    distinct = max(_whole(result.get('distinct')), len(accepted))
    return {
        'title': _text(result.get('title')),
        'url': observed_url(page, page, owner_user_id),
        'total': max(_whole(result.get('total')), distinct),
        'distinct': distinct,
        'items': accepted[:MEDIA_INVENTORY_MAX_ITEMS],
    }


def render_media_inventory(inventory: dict, *, tab_id) -> str:
    """One line per distinct resource plus how to acquire each entry."""
    items = inventory['items']
    total = inventory['total']
    distinct = inventory['distinct']
    head = f'Page media (tab {tab_id})'
    lines = [head + (f': {inventory["title"]}' if inventory['title'] else '')]
    if inventory['url']:
        lines.append(f'URL: {inventory["url"]}')
    if not total:
        lines.append(
            'No rendered <img>/<video>/<audio> elements. CSS background images, '
            'canvas drawings and iframe content are not inventoried; use '
            'browser_screenshot to see them.')
        return '\n'.join(lines)
    summary = f'{total} media elements'
    if distinct < total:
        summary += f' ({distinct} distinct)'
    summary += ', video/audio first, then images, largest first'
    if len(items) < distinct:
        summary += (f'; showing {len(items)} of {distinct} (truncated at the '
                    f'{MEDIA_INVENTORY_MAX_ITEMS}-record receipt bound)')
    lines.append(summary + ':')
    for number, item in enumerate(items, 1):
        size = ('hidden' if item['hidden']
                else f'{round(item["width"])}x{round(item["height"])}px')
        line = f'[{number}] {item["kind"]} {size}'
        if item['count'] > 1:
            line += f' x{item["count"]}'
        if item['intrinsic_width'] and item['intrinsic_height']:
            line += f' (intrinsic {item["intrinsic_width"]}x{item["intrinsic_height"]})'
        if item['duration'] is not None:
            line += f' {item["duration"]:.1f}s'
        if item['in_viewport']:
            line += ' in-viewport'
        if item['label']:
            line += ' ' + json.dumps(item['label'], ensure_ascii=False)
        # A shown url stages the item by itself, so only a withheld url needs
        # the selector; an unfetchable item can only say why.
        if item['source'] != 'http':
            line += f' | not stageable: {item["source"]} ({item["reason"]})'
        elif item['url']:
            line += f' | url: {item["url"]}'
        else:
            line += (f' | url: {item["reason"]} | selector: '
                     f'{item["selector"] or "(none: not addressable)"}')
        if item['poster']:
            line += f' | poster: {item["poster"]}'
        lines.append(line)
    lines.append(
        'Stage an item with browser_download_url_to_server(url=<url>) when its url '
        'is shown, or browser_download_url_to_server(selector=<selector>, '
        f'tab_id={tab_id}) when it is withheld, then read the staged path with '
        'inspect_image (image) or inspect_video (video or audio). '
        'blob/data/stream/none items have no fetchable URL; use '
        'browser_screenshot for a visual frame.')
    return '\n'.join(lines)


__all__ = ['MEDIA_ELEMENT_JS', 'MEDIA_INVENTORY_MAX_ITEMS', 'accept_media_inventory',
           'media_inventory_script', 'render_media_inventory']
