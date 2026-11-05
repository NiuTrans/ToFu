"""Embedded page data islands: a fixed page reader, pure parsers, acceptance seam.

Responsibility: read the structured data modern pages embed in their HTML -
JSON-typed <script> elements, React Server Components flight streams pushed
through ``self.<name>.push([n, "..."])``, and window globals assigned by
inline scripts - on any page, without site adapters and without evaluating
page JavaScript (JSON.parse / json.loads only). Decoded islands become
``hydrated_state`` sources of the sole acceptance owner
(``content.collect_content``), so relevance, cleaning, instruction filtering,
redaction, URL admission and budgets are exactly those of captured API data;
raw island JSON never reaches the model.

Entry points:
  data_islands_script()   the fixed, server-authored, read-only page script
                          sent through the extension's existing execute_js
                          command (page world; JSON and flight text still read
                          when a page's CSP confines it to the isolated world).
  parse_flight_rows()     React Flight text rows -> (id, kind, value).
  parse_islands()         untrusted script result -> deduplicated JSON
                          payloads plus honest coverage counts.
  with_islands()          a read payload whose hydrated sources include them.
  render_islands_report() coverage note, plus value-free island shapes when
                          nothing was accepted.

Dependencies: lib.browser.content (collector bounds, WorkBudget, clean_text),
lib.browser.content_contract (receipt title bound) and
lib.browser.network_evidence (shape inference and rendering). The read handler
(handlers/_page.py) owns transport and re-checks the reported page URL with
handlers/_tabs._result_url_allowed before parsing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from . import network_evidence as net
from .content import (
    CLEAN_MAX_DEPTH,
    HYDRATED_STATE_MAX_SOURCES,
    SOURCE_TEXT_MAX_CHARS,
    WORK_BUDGET_CHARS,
    WorkBudget,
    clean_text,
)
from .content_contract import CONTENT_RECEIPT_SCHEMA, ReadTarget

#: An island name is shown like a record title, so it shares that bound.
_NAME_MAX_CHARS = CONTENT_RECEIPT_SCHEMA['properties']['records']['items'][
    'properties']['title']['maxLength']
_KINDS = frozenset({'json', 'flight', 'global'})
_UNDECODED = object()
_DROPPED = object()
# React writes lowercase hex row ids, so a leading capital is a legacy tag
# (React 18.2: 'J7:' model, 'M2:' module, 'S8:' symbol, 'E3:' error).
_ROW_START = re.compile(r'([A-Z]?)([0-9a-f]*):')
_TEXT_ROW = re.compile(r'T([0-9a-fA-F]+),')
_JSON_START = frozenset('[{"-0123456789')
_JSON_LITERALS = frozenset({'true', 'false', 'null'})
_REFERENCE = re.compile(r'\$([0-9a-f]+)')
_LAZY_REFERENCE = re.compile(r'@([0-9a-f]+)')

# Read-only: no DOM writes, navigation, network, timers or evaluation. Inline
# script text is only matched and JSON.parse'd; globals are read as own data
# properties (accessors on the global object are never invoked) and projected
# like JSON serialization (nested accessors and Proxy traps run, as there),
# omitting functions, DOM nodes, windows and binary buffers. The projection
# spends the bound on plain data across the whole island before any class
# instance, projects each object once, and reads a Vue ref as its raw value.
_PAGE_SCRIPT = r"""(() => {
  const bounds = __BOUNDS__;
  const jsonType = /^(?:application\/json|[\w!#$&^.+-]+\/[\w!#$&^.+-]*\+json)$/;
  const scriptType = /^(?:|module|(?:text|application)\/(?:x-)?(?:java|ecma)script)$/;
  const pushCall = /^\s*(?:self|window|globalThis)\.([A-Za-z_$][\w$]*)\.push\(([\s\S]*)\)\s*;?\s*$/;
  const assignment = /(?:^|[;{}\n])\s*(?:window|self|globalThis)\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*(["'])([^"'\\\n]+)\2\s*\])\s*=(?![=>])/g;
  const ownData = (name) => {
    try {
      const descriptor = Object.getOwnPropertyDescriptor(window, name);
      return descriptor && 'value' in descriptor ? descriptor : null;
    } catch (_error) {
      return null;
    }
  };
  const isChunk = (entry) => Array.isArray(entry) && Number.isInteger(entry[0])
    && typeof entry[1] === 'string';
  const isHost = (value) => {
    try {
      return value === window || value.window === value || value instanceof Node
        || (typeof value.nodeType === 'number' && typeof value.nodeName === 'string')
        || value instanceof ArrayBuffer || ArrayBuffer.isView(value);
    } catch (_error) {
      return true;
    }
  };
  // Vue 3 isRef() is an own __v_isRef === true; ref() and shallowRef() keep
  // the raw value in own _rawValue (_value is its reactive proxy and dep its
  // subscriber graph), so such a ref is read as that raw value.
  const unref = (value) => {
    if (value === null || typeof value !== 'object') return value;
    const marker = Object.getOwnPropertyDescriptor(value, '__v_isRef');
    const raw = marker && marker.value === true
      ? Object.getOwnPropertyDescriptor(value, '_rawValue') : undefined;
    return raw && 'value' in raw ? raw.value : value;
  };
  // Plain data: arrays and objects whose prototype is Object.prototype or null
  // (a Proxy reports its target's); a Date is data as its ISO text.
  const isData = (value) => {
    if (Array.isArray(value) || value instanceof Date) return true;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  };
  // Depth-first in key order through plain data, from any object. A class
  // instance (framework runtime state, Map/Set, components, ...) waits until
  // the island's plain data is projected; deferred instances then fill their
  // places tier by tier with the budget the data left.
  const project = (root, limit) => {
    let remaining = limit;
    let truncated = false;
    let tier = 0;
    let deferred = [];
    const visited = new Set();
    const reserved = new Set();
    // Only a cut of plain data truncates the island; runtime objects are
    // projected best-effort.
    const cut = () => {
      truncated = truncated || tier === 0;
    };
    // The collector cuts every value deeper than maxDepth, scalars included.
    const visit = (value, depth) => {
      const type = value === null ? 'null' : typeof value;
      if (type === 'object' ? visited.has(value) || isHost(value)
        : !['null', 'boolean', 'number', 'string'].includes(type)) return undefined;
      if (depth > bounds.maxDepth) {
        cut();
        return undefined;
      }
      if (type === 'string') {
        const text = value.slice(0, Math.max(0, remaining));
        if (text.length < value.length) cut();
        remaining -= text.length + 2;
        return text;
      }
      if (type !== 'object') {
        remaining -= String(value).length;
        return type === 'number' && !Number.isFinite(value) ? null : value;
      }
      try {
        if (value instanceof Date) {
          const iso = value.toISOString();
          remaining -= iso.length + 2;
          return iso;
        }
        visited.add(value);
        const array = Array.isArray(value);
        const keys = array ? null : Object.keys(value);
        const count = array ? value.length : keys.length;
        const out = array ? [] : {};
        remaining -= 2;
        for (let index = 0; index < count; index += 1) {
          if (remaining <= 0) {
            cut();
            break;
          }
          const key = array ? index : keys[index];
          // Every array slot costs its separator, so holes cannot spin unbounded.
          if (array) remaining -= 1;
          let child;
          let later;
          try {
            child = unref(value[key]);
            later = child !== null && typeof child === 'object' && !isData(child);
          } catch (_error) {
            continue;
          }
          if (later) {
            if (visited.has(child) || isHost(child)) continue;
            if (array) {
              reserved.add(out);
              out.push(undefined);
            }
            deferred.push({holder: out, key: array ? out.length - 1 : key, value: child,
              depth: depth + 1});
            continue;
          }
          const projected = visit(child, depth + 1);
          if (projected === undefined) continue;
          if (array) {
            out.push(projected);
          } else {
            out[key] = projected;
            remaining -= String(key).length + 4;
          }
        }
        return out;
      } catch (_error) {
        return undefined;
      }
    };
    let value;
    try {
      value = visit(unref(root), 0);
    } catch (_error) {
      value = undefined;
    }
    while (deferred.length > 0 && remaining > 0) {
      const batch = deferred;
      deferred = [];
      tier += 1;
      for (const entry of batch) {
        if (remaining <= 0) break;
        const projected = visit(entry.value, entry.depth);
        if (projected === undefined) continue;
        entry.holder[entry.key] = projected;
        if (typeof entry.key !== 'number') remaining -= String(entry.key).length + 4;
      }
    }
    for (const array of reserved) {
      let kept = 0;
      for (let index = 0; index < array.length; index += 1) {
        if (array[index] !== undefined) {
          array[kept] = array[index];
          kept += 1;
        }
      }
      array.length = kept;
    }
    return {value, truncated, used: Math.min(limit, limit - remaining)};
  };
  const json = [];
  const streams = new Map();
  const assigned = new Map();
  Array.from(document.scripts).forEach((script, position) => {
    if (script.hasAttribute('src')) return;
    const type = (script.getAttribute('type') || '').split(';')[0].trim().toLowerCase();
    const text = script.textContent || '';
    if (jsonType.test(type)) {
      if (text.trim()) {
        json.push({kind: 'json', name: script.id || type, type, text, position,
          first: type === 'application/ld+json'});
      }
      return;
    }
    if (!scriptType.test(type)) return;
    const push = pushCall.exec(text);
    if (push) {
      const stream = streams.get(push[1]) || {name: push[1], position, markup: []};
      streams.set(push[1], stream);
      let entry = null;
      try {
        entry = JSON.parse(push[2]);
      } catch (_error) {
        stream.markup.push(null);
        return;
      }
      if (isChunk(entry)) stream.markup.push(entry[1]);
      return;
    }
    assignment.lastIndex = 0;
    for (let match = assignment.exec(text); match; match = assignment.exec(text)) {
      const name = match[1] || match[3];
      if (!assigned.has(name)) assigned.set(name, position);
    }
  });
  let world = 'page';
  try {
    if (typeof chrome === 'object' && chrome && chrome.runtime && chrome.runtime.id) {
      world = 'isolated';
    }
  } catch (_error) {
    world = 'page';
  }
  const candidates = json.slice();
  for (const stream of streams.values()) {
    const own = ownData(stream.name);
    const live = [];
    if (own && Array.isArray(own.value)) {
      for (const entry of own.value) {
        if (isChunk(entry)) live.push(entry[1]);
      }
    }
    // The live array is complete only when it accounts for every push in
    // the markup; runtimes that replace push() keep just a prefix of it.
    const aligned = live.length > 0 && stream.markup.every((chunk, index) =>
      chunk === null || index >= live.length || chunk === live[index]);
    let chunks = stream.markup;
    let origin = 'markup';
    if (aligned && live.length >= stream.markup.length) {
      chunks = live;
      origin = 'live';
    } else if (aligned) {
      chunks = stream.markup.map((chunk, index) =>
        chunk === null && index < live.length ? live[index] : chunk);
    }
    const present = chunks.filter((chunk) => chunk !== null);
    if (present.length) {
      candidates.push({kind: 'flight', name: stream.name, position: stream.position,
        first: false, chunks: present, origin, missing: chunks.length - present.length});
    }
  }
  for (const [name, position] of assigned) {
    if (streams.has(name)) continue;
    const own = ownData(name);
    const value = own ? own.value : null;
    if (typeof value === 'string' || (value && typeof value === 'object' && !isHost(value))) {
      candidates.push({kind: 'global', name, position, first: false, value});
    }
  }
  candidates.sort((a, b) => (b.first - a.first) || (a.position - b.position));
  let remaining = bounds.maxTotalChars;
  const islands = candidates.slice(0, bounds.maxSources).map((candidate) => {
    // A flight stream is a sequence of independent rows; the server bounds
    // each row, so the stream itself is bounded only by the total.
    const limit = Math.max(0, candidate.kind === 'flight' ? remaining
      : Math.min(bounds.maxSourceChars, remaining));
    const island = {kind: candidate.kind, name: String(candidate.name).slice(0, bounds.maxName)};
    if (candidate.kind === 'json') {
      island.type = candidate.type;
      island.text = candidate.text.slice(0, limit);
      island.length = candidate.text.length;
      island.truncated = island.text.length < candidate.text.length;
      remaining -= island.text.length;
    } else if (candidate.kind === 'flight') {
      island.origin = candidate.origin;
      island.missing = candidate.missing;
      island.chunks = [];
      island.length = 0;
      let used = 0;
      for (const chunk of candidate.chunks) {
        island.length += chunk.length;
        const piece = chunk.slice(0, limit - used);
        used += piece.length;
        if (piece) island.chunks.push(piece);
      }
      island.truncated = used < island.length;
      remaining -= used;
    } else {
      const projected = project(candidate.value, limit);
      island.value = projected.value;
      island.truncated = projected.truncated;
      remaining -= projected.used;
    }
    return island;
  });
  return {url: location.href, title: document.title, world, total: candidates.length, islands};
})()"""


def data_islands_script() -> str:
    """Return the fixed read-only island reader; its bounds are the collector's."""
    bounds = json.dumps({
        'maxSources': HYDRATED_STATE_MAX_SOURCES,
        'maxSourceChars': SOURCE_TEXT_MAX_CHARS,
        'maxTotalChars': WORK_BUDGET_CHARS,
        'maxDepth': CLEAN_MAX_DEPTH,
        'maxName': _NAME_MAX_CHARS,
    })
    return _PAGE_SCRIPT.replace('__BOUNDS__', bounds)


@dataclass(frozen=True)
class Island:
    """One decoded payload; a flight row decoded apart is '<stream>#<row id>'."""

    kind: str
    name: str
    value: object
    truncated: bool = False


@dataclass(frozen=True)
class IslandRead:
    """The decoded islands of one page and honest coverage counts."""

    page_url: str
    available: bool = True
    reason: str = ''
    payloads: tuple[Island, ...] = ()
    found: int = 0
    kinds: tuple[tuple[str, int], ...] = ()
    truncated: int = 0
    malformed: int = 0
    duplicates: int = 0
    omitted: int = 0
    missing_chunks: int = 0
    isolated: bool = False

    @classmethod
    def unavailable(cls, page_url: str, reason: str) -> 'IslandRead':
        return cls(page_url=page_url, available=False, reason=reason)


def _looks_like_json(text: str) -> bool:
    head = text.lstrip()[:3].lower()
    return head[:1] in ('{', '[') or head in ('%7b', '%5b')


def _decode_json_text(text: str):
    """JSON text; URL-decoded first only when it starts with an encoded '{' or '['."""
    text = text.strip()
    if text[:3].lower() in ('%7b', '%5b'):
        text = unquote(text).strip()
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return _UNDECODED


def _utf8_end(text: str, start: int, size: int):
    """Index after ``size`` UTF-8 bytes of text[start:], or None when it ends first."""
    candidate = text[start:start + size]
    if candidate.isascii():
        return start + size if len(candidate) == size else None
    position = start
    while size > 0:
        if position >= len(text):
            return None
        code = ord(text[position])
        size -= 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
        position += 1
    return position


def _json_row(row_id: str, kind: str, payload: str):
    """One JSON row, or a 'cut' marker past the per-source text bound."""
    if len(payload) > SOURCE_TEXT_MAX_CHARS:
        return row_id, 'cut', None
    try:
        return row_id, kind, json.loads(payload)
    except (ValueError, RecursionError):
        return None


def parse_flight_rows(text: str) -> list[tuple[str, str, object]]:
    """React Flight rows of one concatenated stream, in stream order.

    Current grammar: ``<hex id>:<json>`` lines are 'model' rows, and
    ``<id>:T<hex length>,<text>`` consumes exactly that many UTF-8 bytes
    (React writes the byte length; the text may contain newlines and no
    newline follows it). Letter-tagged payloads (I modules, HL hints, D debug,
    E errors, ...) carry no page data and are skipped. Legacy grammar (React
    18.2) puts the tag before the id: ``J<id>:<json>`` rows are 'legacy'
    models; M, S, E and other tags are skipped. Each row is its own source
    text: one longer than SOURCE_TEXT_MAX_CHARS is reported as 'cut' and not
    parsed. Malformed rows and an incomplete tail are skipped.
    """
    rows = []
    position = 0
    length = len(text)
    while position < length:
        if text[position] == '\n':
            position += 1
            continue
        start = _ROW_START.match(text, position)
        newline = text.find('\n', position)
        line_end = length if newline < 0 else newline
        if start is None:
            position = line_end + 1
            continue
        tag, row_id = start.groups()
        text_row = None if tag else _TEXT_ROW.match(text, start.end())
        if text_row is not None:
            end = _utf8_end(text, text_row.end(), int(text_row.group(1), 16))
            if end is None:
                break
            body = text[text_row.end():end]
            rows.append((row_id, 'cut', None) if len(body) > SOURCE_TEXT_MAX_CHARS
                        else (row_id, 'text', body))
            position = end
            continue
        payload = text[start.end():line_end]
        position = line_end + 1
        if tag == 'J' or (not tag and (payload[:1] in _JSON_START or payload in _JSON_LITERALS)):
            row = _json_row(row_id, 'legacy' if tag else 'model', payload)
            if row is not None:
                rows.append(row)
    return rows


def _flight_string(text: str, strings: dict, legacy: bool):
    """One Flight-encoded string as its value, or _DROPPED.

    '$$x' is the literal '$x' (legacy also '$@x' for '@x'); a '$<hex id>'
    reference (legacy also '@<hex id>') naming a string row is that string;
    '$D' dates and '$n' big integers keep their text, '$-0' is 0 and
    NaN/Infinity become null as in JSON. '$undefined' and every unresolved
    reference (elements, lazy chunks, promises, modules, symbols) are
    dropped, so encoding never surfaces as a title or snippet.
    """
    if not (text.startswith('$') or (legacy and text.startswith('@'))) or text == '$':
        return text
    if text.startswith('$$') or (legacy and text.startswith('$@')):
        return text[1:]
    reference = _REFERENCE.fullmatch(text) or (legacy and _LAZY_REFERENCE.fullmatch(text))
    if reference:
        return strings.get(reference.group(1), _DROPPED)
    if text in ('$NaN', '$Infinity', '$-Infinity'):
        return None
    if text == '$-0':
        return 0
    if text[1:2] in ('D', 'n'):
        return text[2:]
    return _DROPPED


def _decode_flight_values(value, strings: dict, *, legacy: bool):
    """Decode every string of parsed model rows in place, removing drops."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in list(node):
                child = node[key]
                if isinstance(child, str):
                    child = _flight_string(child, strings, legacy)
                    if child is _DROPPED:
                        del node[key]
                    else:
                        node[key] = child
                elif isinstance(child, (dict, list)):
                    stack.append(child)
            continue
        kept = []
        for child in node:
            if isinstance(child, str):
                child = _flight_string(child, strings, legacy)
                if child is _DROPPED:
                    continue
            elif isinstance(child, (dict, list)):
                stack.append(child)
            kept.append(child)
        node[:] = kept
    return value


def _flight_payloads(name: str, text: str) -> tuple[list[tuple[str, object]], int]:
    """Model rows as one payload; JSON or URL-encoded JSON rows as their own.

    Returns the payloads and how many rows exceeded the per-source bound.
    """
    if _looks_like_json(text):
        if len(text) > SOURCE_TEXT_MAX_CHARS:
            return [], 1
        value = _decode_json_text(text)
        if value is not _UNDECODED:
            return [(name, value)], 0
    rows = parse_flight_rows(text)
    legacy = any(kind == 'legacy' for _row_id, kind, _value in rows)
    payloads, models, strings, cut = [], [], {}, 0
    for row_id, kind, value in rows:
        cut += kind == 'cut'
        if isinstance(value, str) and _looks_like_json(value):
            decoded = _decode_json_text(value)
            if decoded is not _UNDECODED:
                payloads.append((f'{name}#{row_id}', decoded))
                continue
        if kind == 'text':
            # Text rows are raw text, never Flight-encoded.
            strings[row_id] = value
        elif isinstance(value, str):
            plain = _flight_string(value, {}, legacy)
            if plain is not _DROPPED and isinstance(plain, str):
                strings[row_id] = plain
        elif isinstance(value, (dict, list)):
            models.append(value)
    if models:
        payloads.insert(0, (name, _decode_flight_values(models, strings, legacy=legacy)))
    return payloads, cut


def _same_document(url, page_url: str) -> bool:
    if not isinstance(url, str) or not url or not page_url:
        return False
    try:
        return (urlsplit(url)._replace(fragment='')
                == urlsplit(page_url)._replace(fragment=''))
    except ValueError:
        return False


def _name(value) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(clean_text(value[:_NAME_MAX_CHARS]).split())


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError):
        return object()
    return hashlib.sha256(encoded.encode('ascii')).digest()


def parse_islands(result, *, page_url: str) -> IslandRead:
    """Decode one untrusted reader result; malformed islands are counted, never raised.

    The page's own bounds are re-applied: the first HYDRATED_STATE_MAX_SOURCES
    islands, SOURCE_TEXT_MAX_CHARS per JSON text (a JSON island, a global's
    string, each flight row; a flight stream is a sequence of independent
    rows) and WORK_BUDGET_CHARS in total. Identical payloads (an island
    repeated inside a flight stream) are kept once, by content not by name.
    """
    if not isinstance(result, dict):
        return IslandRead.unavailable(page_url, 'the reader returned no result')
    if not _same_document(result.get('url'), page_url):
        return IslandRead.unavailable(page_url, 'the page changed between reads')
    entries = result.get('islands') if isinstance(result.get('islands'), list) else []
    reported = result.get('total')
    found = max(reported if type(reported) is int else 0, len(entries))
    counts, kinds = Counter(), Counter()
    payloads, digests = [], set()
    remaining = WORK_BUDGET_CHARS
    for entry in entries[:HYDRATED_STATE_MAX_SOURCES]:
        if not isinstance(entry, dict) or entry.get('kind') not in _KINDS:
            continue
        kind = entry['kind']
        name = _name(entry.get('name')) or kind
        cut = entry.get('truncated') is True
        value = entry.get('value')
        if kind == 'flight':
            chunks = entry.get('chunks') if isinstance(entry.get('chunks'), list) else []
            text = ''.join(chunk for chunk in chunks if isinstance(chunk, str))
            missing = entry.get('missing')
            counts['missing_chunks'] += missing if type(missing) is int and missing > 0 else 0
        elif kind == 'json':
            text = entry.get('text') if isinstance(entry.get('text'), str) else ''
        elif isinstance(value, str):
            if not _looks_like_json(value):
                continue
            text = value
        elif isinstance(value, (dict, list)):
            text = None
        else:
            continue
        if text is not None:
            limit = max(0, remaining)
            if kind != 'flight':
                limit = min(SOURCE_TEXT_MAX_CHARS, limit)
            cut |= len(text) > limit
            text = text[:limit]
            remaining -= len(text)
            if kind == 'flight':
                decoded, oversized = _flight_payloads(name, text)
                cut |= oversized > 0
            else:
                value = _decode_json_text(text)
                decoded = [] if value is _UNDECODED else [(name, value)]
        else:
            decoded = [(name, value)]
        if not decoded:
            counts['truncated' if cut else 'malformed'] += 1
            continue
        kinds[kind] += 1
        counts['truncated'] += cut
        for payload_name, payload in decoded:
            digest = _digest(payload)
            if digest in digests:
                counts['duplicates'] += 1
                continue
            digests.add(digest)
            payloads.append(Island(kind, payload_name, payload, cut))
    omitted = (found - min(len(entries), HYDRATED_STATE_MAX_SOURCES)
               + max(0, len(payloads) - HYDRATED_STATE_MAX_SOURCES))
    return IslandRead(
        page_url=page_url, payloads=tuple(payloads[:HYDRATED_STATE_MAX_SOURCES]),
        found=found, kinds=tuple(sorted(kinds.items())), truncated=counts['truncated'],
        malformed=counts['malformed'], duplicates=counts['duplicates'], omitted=omitted,
        missing_chunks=counts['missing_chunks'], isolated=result.get('world') == 'isolated')


def with_islands(payload: dict, islands: IslandRead) -> dict:
    """A copy of one read payload whose hydrated sources include the islands."""
    if not islands.payloads:
        return payload
    merged = dict(payload)
    states = merged.get('initialStatePayloads')
    states = (list(states.values()) if isinstance(states, dict)
              else list(states) if isinstance(states, list) else [])
    # The wrapper keeps an island whose own keys include value/data/text from
    # being unwrapped by the collector's source-shape probe.
    merged['initialStatePayloads'] = states + [{'value': island.value}
                                               for island in islands.payloads]
    if islands.truncated or islands.omitted or islands.missing_chunks:
        merged['truncated'] = True
    return merged


def render_islands_note(read: IslandRead) -> str:
    """One line naming what was read and every bound or failure that cut it."""
    if not read.available:
        return f'Embedded data islands unavailable: {read.reason}.'
    if not read.found:
        return 'Embedded data islands: none found.'
    kinds = ', '.join(f'{kind} {count}' for kind, count in read.kinds)
    parts = [f'{sum(count for _, count in read.kinds)} read' + (f' ({kinds})' if kinds else '')]
    if read.duplicates:
        parts.append(f'{read.duplicates} duplicate payload(s) skipped')
    if read.truncated:
        parts.append(f'{read.truncated} cut at the {SOURCE_TEXT_MAX_CHARS}-character '
                     f'per-source or {WORK_BUDGET_CHARS}-character total bound')
    if read.malformed:
        parts.append(f'{read.malformed} not parseable as JSON or flight rows')
    if read.omitted:
        parts.append(f'{read.omitted} beyond the {HYDRATED_STATE_MAX_SOURCES}-source bound')
    if read.missing_chunks:
        parts.append(f'{read.missing_chunks} flight chunk(s) unreadable')
    if read.isolated:
        parts.append('page globals unreadable (the reader ran in the extension '
                     'isolated world)')
    return 'Embedded data islands: ' + '; '.join(parts) + '.'


def render_islands_report(read: IslandRead, receipt: dict, *, target=None) -> str:
    """The coverage note; when nothing was accepted, each island's shape too.

    Shapes come from the cleaned island (private subtrees and secret-named
    keys removed) and carry types only, so the model learns what structure
    exists without raw JSON entering its context.
    """
    note = render_islands_note(read)
    if not read.payloads or receipt.get('records') or receipt.get('content'):
        return note
    target = ReadTarget.from_value(target)
    lines = [note, 'Island shapes (types only; no island supplied records for this target):']
    for island in read.payloads:
        cleaned = WorkBudget().clean(island.value, target, read.page_url)
        lines.append(f'- {island.kind} {island.name}: '
                     f'{net.render_shape(net._infer_shape(cleaned))}')
    return '\n'.join(lines)


__all__ = ['Island', 'IslandRead', 'data_islands_script', 'parse_flight_rows',
           'parse_islands', 'render_islands_note', 'render_islands_report',
           'with_islands']
