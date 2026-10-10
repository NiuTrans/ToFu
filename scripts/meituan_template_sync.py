#!/usr/bin/env python3
"""scripts/yourprovider_template_sync.py — Sync yourprovider.json to the ModelPlaza catalog.

Authority chain: ModelPlaza (SSO-gated catalog of record,
``https://api.openai.com/ml/modelPlaza/modelInfo``) x live-gateway probe
(API key, served proof) -> template recipes. Neither source suffices alone:

  * Plaza listing is NOT served proof — dead ids linger (text-embedding-v4,
    gpt-image-1.5 listed but 404 on their surfaces; claude-sonnet-4.6 lists
    but 400s "Access to Anthropic models is not allowed for this account").
  * The gateway's OpenAI ``/models`` returns an empty list, so probe-only
    discovery cannot see newly listed ids.

The template is the intersection, with curated per-recipe fields
(request_ids pools, rpm/cost, context-window overrides) preserved verbatim
for surviving recipes. Policy exclusions (EXCLUDED) win over the probe:
the retired original GPT-5 family stays out even though the gateway serves
it, per the dated note in lib/llm_dispatch/config/_slots.py.

Probe routing reuses ``lib.provider_probe.build_probe_work`` so a Claude
candidate is probed on the Anthropic face exactly the way the dispatcher
would route it — never a re-derived face guess.

Chat surface only. Embedding / image-gen / ASR / video plaza entries are
classified and reported but never probed on their own surfaces nor added
as recipes: the functional defaults (IMAGE_GEN_MODEL / EMBEDDING_MODELS)
are an owner decision, and image probes bill a generation each.

Usage:
  # one command: fetch plaza with an SSO cookie, probe live, print report
  MEITUAN_SSO_COOKIE='tokenPairs=...' python3 scripts/yourprovider_template_sync.py

  # apply to bundles/yourprovider/provider_templates/yourprovider.json + lib/pricing/_tables.py
  MEITUAN_SSO_COOKIE=... python3 scripts/yourprovider_template_sync.py --write

  # offline iteration: saved plaza response + saved verdicts
  python3 scripts/yourprovider_template_sync.py --snapshot plaza.json --verdicts-in v.json

The cookie is the browser session for your-llm-gateway.example.com (DevTools ->
Application -> Cookies, or copy the Cookie header from any plaza XHR).
Verdicts are cached via --verdicts-out/--verdicts-in so developing the
report never re-bills probes.

Tests: tests/test_yourprovider_template_sync.py
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TEMPLATE_PATH = os.path.join(
    _ROOT, 'bundles', 'yourprovider', 'provider_templates', 'yourprovider.json')
_PRICING_PATH = os.path.join(_ROOT, 'lib', 'pricing', '_tables.py')
_CONFIG_PATH = os.path.join(_ROOT, 'data', 'config', 'server_config.json')

_PLAZA_API = '/api/friday/ml/v1/model/series/information/listV2'
_PLAZA_ORIGIN = 'https://api.openai.com'

# Policy exclusions: never added as recipes even when the probe proves the
# gateway serves them. The retired original GPT-5 family per the dated note
# in _slots.py; Ring/Ling-1T (Ant Bailing) 400 on both faces but stay listed
# here so a gateway fix never silently resurrects a dead slot.
EXCLUDED = frozenset({
    'gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5-mini', 'gpt-5-nano',
    'gpt-5.1-nothinking',
    'Ring-1T', 'Ling-1T', 'ring-1t', 'ling-1t', 'Ring-1T-Flash',
})

_SERVED_VERDICTS = frozenset({'ok', 'rate_limited'})
_TRANSIENT_VERDICTS = frozenset(
    {'unavailable', 'error', 'invalid_response'})

_SKIP_PAT = re.compile(
    r'(sora|veo|seedance|cogvideo|pixverse|video|t2v|i2v|ti2v|asr|tts'
    r'|audio)', re.I)

# Image/embedding generators the generic name patterns in
# discovery._capabilities miss — they probe "ok" on chat because this
# gateway serves image generation through chat completions, but they are
# not chat recipes.
_IMAGE_EXTRA_PAT = re.compile(
    r'(seedream|seededit|byteedit|cogview|jimeng|t2i|high_aes_general)',
    re.I)
_EMBED_EXTRA_PAT = re.compile(r'(embo[-_]?\d|miffy)', re.I)

_CTX_RE = re.compile(r'^\s*([\d.]+)\s*([kKmM]?)\s*$')
_RPM_RE = re.compile(r'(\d+)')

_CNY_PER_USD = 7.2

_PRICING_BEGIN = '    # -- BEGIN yourprovider_template_sync auto-added (regenerated wholesale) --\n'
_PRICING_END = '    # -- END yourprovider_template_sync auto-added --\n'


# ── Plaza fetch / projection ─────────────────────────────────────────────

def fetch_plaza(cookie: str, origin: str = _PLAZA_ORIGIN,
                page_size: int = 100, timeout: int = 20) -> list[dict]:
    """POST the plaza listV2 API page by page until totalCount is covered."""
    entries: list[dict] = []
    page = 1
    total = None
    while total is None or len(entries) < total:
        body = json.dumps({'pageNum': page, 'pageSize': page_size}).encode()
        req = urllib.request.Request(
            origin + _PLAZA_API, data=body, method='POST',
            headers={'Content-Type': 'application/json', 'Cookie': cookie})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise SystemExit('plaza fetch HTTP %d: %s'
                             % (e.code, e.read()[:200]))
        code = payload.get('code')
        if code not in (0, '0', 200, '200', None):
            raise SystemExit('plaza fetch rejected: code=%r message=%r — '
                             'SSO cookie missing/expired?'
                             % (code, payload.get('message')))
        data = payload.get('data') or {}
        batch = data.get('list') or []
        total = data.get('totalCount') or len(batch)
        if not batch:
            break
        entries.extend(batch)
        page += 1
        if page > 50:
            raise SystemExit('plaza pagination runaway (>50 pages)')
    return entries[:total]


def load_snapshot(path: str) -> list[dict]:
    """Accept a saved listV2 response, a bare entry list, {'pages': []},
    or the compact projection ({n/w/cat/ctx/in/out/rpm/tags}) produced by a
    browser-side dump."""
    raw = json.load(open(path, encoding='utf-8'))
    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict) and 'w' in raw[0]:
            return [{'modelCallName': e.get('w'), 'name': e.get('n'),
                     'modelCategory': e.get('cat'),
                     'contextLength': e.get('ctx'),
                     'defaultRpm': str(e.get('rpm') or ''),
                     'inputPrice': str(e.get('in') or ''),
                     'outputPrice': str(e.get('out') or ''),
                     'tags': e.get('tags') or []} for e in raw]
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get('pages'), list):
            out: list[dict] = []
            for p in raw['pages']:
                out.extend(load_snapshot_obj(p))
            return out
        return load_snapshot_obj(raw)
    raise SystemExit('unrecognized snapshot shape: %s' % type(raw).__name__)


def load_snapshot_obj(raw: dict) -> list[dict]:
    data = raw.get('data')
    if isinstance(data, dict) and isinstance(data.get('list'), list):
        return data['list']
    if isinstance(raw.get('list'), list):
        return raw['list']
    raise SystemExit('snapshot page has no data.list')


def parse_ctx(text) -> int | None:
    """'1024K' -> 1024000, '1M' -> 1000000, '0.512K' -> 512, '未知' -> None.

    K/M follow the template's decimal convention (128K -> 128000).
    """
    m = _CTX_RE.match(str(text or ''))
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == 'k':
        value *= 1000
    elif unit == 'm':
        value *= 1000000
    result = int(value)
    return result if result >= 256 else None


def parse_rpm(text) -> int | None:
    """'100（美团独立集群）' -> 100; '0'/'' -> None (0 = no self-serve quota)."""
    m = _RPM_RE.search(str(text or ''))
    if not m:
        return None
    rpm = int(m.group(1))
    return rpm if rpm > 0 else None


def _parse_price(text) -> float | None:
    try:
        value = float(str(text or '').strip())
    except ValueError:
        return None
    return value if value > 0 else None


def project_entry(raw: dict) -> dict | None:
    """One plaza entry -> a candidate, or None for series headers (no wire id)."""
    wire = (raw.get('modelCallName') or '').strip()
    if not wire:
        return None
    tags = [t for t in (raw.get('tags') or []) if isinstance(t, str)]
    return {
        'wire': wire,
        'name': (raw.get('name') or wire).strip(),
        'cat': (raw.get('modelCategory') or '').strip(),
        'ctx': parse_ctx(raw.get('contextLength')
                         or raw.get('baseModelContextLength')),
        'rpm': parse_rpm(raw.get('defaultRpm')),
        'cny_in': _parse_price(raw.get('inputPrice')),
        'cny_out': _parse_price(raw.get('outputPrice')),
        'tags': tags,
    }


def classify_surface(candidate: dict) -> str:
    """'chat' | 'embedding' | 'image_gen' | 'skip' (video/audio — no probe)."""
    from lib.llm_dispatch.discovery._capabilities import _infer_capabilities
    wire = candidate['wire']
    caps = _infer_capabilities(wire)
    if caps == {'embedding'} or _EMBED_EXTRA_PAT.search(wire):
        return 'embedding'
    if caps == {'image_gen'} or _IMAGE_EXTRA_PAT.search(wire):
        return 'image_gen'
    if _SKIP_PAT.search(wire):
        return 'skip'
    return 'chat'


# ── Live probe ───────────────────────────────────────────────────────────

def load_gateway() -> tuple[str, str, dict]:
    """(base_url, api_key, extra_headers) from the configured example-corp provider."""
    cfg = json.load(open(_CONFIG_PATH, encoding='utf-8'))
    prov = next((p for p in cfg.get('providers') or []
                 if p.get('id') == 'example-corp'), None)
    if not prov:
        raise SystemExit('no example-corp provider in data/config/server_config.json')
    key = (prov.get('api_keys') or [None])[0]
    if isinstance(key, dict):
        key = key.get('key') or key.get('api_key') or key.get('value')
    if not key:
        raise SystemExit('example-corp provider has no usable api_keys[0]')
    extra = dict(prov.get('extra_headers') or {})
    tpl = _load_template()
    extra.update(tpl.get('extra_headers') or {})
    return ((prov.get('base_url') or tpl['base_url']).rstrip('/'), key, extra)


def probe_wires(wires: list[str], base_url: str, api_key: str,
                extra_headers: dict, *, workers: int = 8,
                timeout: int = 20) -> dict[str, tuple[str, str]]:
    """wire -> (verdict, detail), routed through build_probe_work so each id
    is probed on the face the dispatcher would use (Claude -> anthropic)."""
    from lib.provider_probe import build_probe_work, probe_one_cell
    provider = {'base_url': base_url, 'protocol': 'openai',
                'faces': _load_template().get('faces') or {}}
    models = [{'model_id': w, 'capabilities': []} for w in wires]
    work = build_probe_work(provider, models, [api_key])
    verdicts: dict[str, tuple[str, str]] = {}

    def _run(item):
        _idx, key, _root, wire, _caps, url, proto = item
        return wire, probe_one_cell(url, key, wire, extra_headers, timeout,
                                    protocol=proto)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for wire, (verdict, detail) in pool.map(_run, work):
            verdicts[wire] = (verdict, detail)
    return verdicts


# ── Merge ────────────────────────────────────────────────────────────────

def _load_template() -> dict:
    return json.load(open(_TEMPLATE_PATH, encoding='utf-8'))


def recipe_wires(recipe: dict) -> list[str]:
    return list(recipe.get('request_ids') or [recipe['model_id']])


def _usd(cny: float) -> float:
    return round(cny / _CNY_PER_USD + 1e-9, 4)


def pricing_row(candidate: dict) -> dict | None:
    if candidate['cny_in'] is None or candidate['cny_out'] is None:
        return None
    return {'input': _usd(candidate['cny_in']),
            'output': _usd(candidate['cny_out']),
            'cacheWriteMul': 1.0, 'cacheReadMul': 0.1,
            'name': candidate['name']}


def _recipe_cost(row: dict | None) -> float:
    if not row:
        return 0.001
    return round((row['input'] + row['output']) / 2000 + 1e-9, 6)


def build_recipe(candidate: dict, row: dict | None) -> dict:
    from lib.llm_dispatch.discovery._capabilities import (
        _infer_capabilities, _infer_rpm)
    caps = _infer_capabilities(candidate['wire'])
    caps.discard('video')  # name-derived video = generator, not input modality
    tags = ' '.join(candidate['tags'])
    if any(k in tags for k in ('多模态', '视觉', '图文', '图片理解', '图像理解')):
        caps.add('vision')
    if '视频理解' in tags:
        caps.add('video')
    if '推理' in tags:
        caps.add('thinking')
    ordered = [c for c in ('text', 'vision', 'video', 'thinking', 'cheap')
               if c in caps]
    recipe = {
        'model_id': candidate['wire'],
        'capabilities': ordered or ['text'],
        'rpm': candidate['rpm'] or _infer_rpm(candidate['wire'], caps),
        'cost': _recipe_cost(row),
    }
    if candidate['ctx']:
        recipe['context_window'] = candidate['ctx']
        recipe['context_window_exact'] = False
        recipe['context_window_source'] = 'yourprovider_plaza'
    return recipe


def _norm_id(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _is_mirror(wire: str, root: str) -> bool:
    """True when `wire` looks like a deployment mirror of recipe `root`.

    Normalized substring (aws./vertex./yuju- prefixes and punctuation-free
    spellings like glm-52-yourprovider) with two guards: roots shorter than 5
    normalized chars never match ('glm-5' -> 'glm5' would false-positive on
    glm-5.3-flash), and a digit immediately after the match kills it
    ('claudefable5' in 'azureclaudefable51' is a different model).
    """
    if wire == root:
        return False
    wn, rn = _norm_id(wire), _norm_id(root)
    if len(rn) < 5 or len(wn) <= len(rn):
        return False
    idx = wn.find(rn)
    while idx >= 0:
        end = idx + len(rn)
        if end >= len(wn) or not wn[end].isdigit():
            return True
        idx = wn.find(rn, idx + 1)
    return False


def merge(template: dict, candidates: dict[str, dict],
          verdicts: dict[str, tuple[str, str]]) -> dict:
    """Returns {kept, added, dropped, unverified, mirror_hints, pricing_rows}.

    A recipe is dropped only when EVERY wire id fails with a definitive
    verdict (not_found / bad_request / unauthorized) or policy excludes it;
    transient failures keep the recipe and flag it unverified."""
    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []
    unverified: list[tuple[str, str]] = []
    referenced: set[str] = set()
    for recipe in template['offering_recipes']:
        wires = recipe_wires(recipe)
        referenced.update(wires)
        referenced.add(recipe['model_id'])
        got = [verdicts.get(w, ('error', ''))[0] for w in wires]
        detail = '; '.join('%s=%s' % (w, v) for w, v in zip(wires, got))
        if recipe['model_id'] in EXCLUDED:
            dropped.append((recipe['model_id'], 'policy-excluded'))
        elif any(v in _SERVED_VERDICTS for v in got):
            kept.append(recipe)
        elif any(v in _TRANSIENT_VERDICTS for v in got):
            kept.append(recipe)
            unverified.append((recipe['model_id'], detail))
        else:
            dropped.append((recipe['model_id'], detail))

    added: list[dict] = []
    mirror_hints: list[str] = []
    pricing_rows: dict[str, dict] = {}
    for wire in sorted(candidates):
        if wire in referenced or wire in EXCLUDED:
            continue
        cand = candidates[wire]
        if classify_surface(cand) != 'chat':
            continue
        if verdicts.get(wire, ('error', ''))[0] not in _SERVED_VERDICTS:
            continue
        mirror = next((r['model_id'] for r in kept
                       if _is_mirror(wire, r['model_id'])), None)
        if mirror:
            mirror_hints.append(
                '%s looks like a mirror of %s — consider request_ids'
                % (wire, mirror))
            continue
        row = pricing_row(cand)
        if row:
            pricing_rows[wire] = row
        added.append(build_recipe(cand, row))

    return {'kept': kept, 'added': added, 'dropped': dropped,
            'unverified': unverified,
            'mirror_hints': mirror_hints, 'pricing_rows': pricing_rows}


# ── Pricing file block ───────────────────────────────────────────────────

def render_pricing_rows(rows: dict[str, dict], cny_notes: dict[str, str]
                        ) -> str:
    out = []
    for wire in sorted(rows):
        r = rows[wire]
        note = cny_notes.get(wire, '')
        out.append(
            "    %r: {'input': %r, 'output': %r, 'cacheWriteMul': 1.0, "
            "'cacheReadMul': 0.1, 'name': %r},%s\n"
            % (wire, r['input'], r['output'], r['name'],
               ('  # plaza ' + note) if note else ''))
    return ''.join(out)


def update_pricing_text(text: str, rows: dict[str, dict],
                        cny_notes: dict[str, str]) -> str:
    """Replace the marked auto block inside MODEL_PRICING; idempotent."""
    pattern = re.compile(re.escape(_PRICING_BEGIN) + '.*?'
                         + re.escape(_PRICING_END), re.S)
    static_text = pattern.sub('', text)
    static_keys = set(re.findall(r"^    '([^']+)':\s*\{", static_text, re.M))
    fresh = {w: r for w, r in rows.items() if w not in static_keys}
    block = _PRICING_BEGIN + render_pricing_rows(fresh, cny_notes) \
        + _PRICING_END
    m = re.search(r'^(MODEL_PRICING\s*=\s*\{)', static_text, re.M)
    if not m:
        raise SystemExit('MODEL_PRICING literal not found in _tables.py')
    close = static_text.index('\n}', m.end())
    return static_text[:close] + '\n' + block + static_text[close + 1:]


# ── Report / main ────────────────────────────────────────────────────────

def _report(merged: dict, non_chat: list[tuple[str, str, str]],
            plaza_wires: set[str], template: dict) -> str:
    lines = []
    lines.append('kept: %d recipes' % len(merged['kept']))
    lines.append('added: %d recipes' % len(merged['added']))
    for r in merged['added']:
        lines.append('  + %s caps=%s rpm=%s cost=%s ctx=%s'
                     % (r['model_id'], '/'.join(r['capabilities']),
                        r['rpm'], r['cost'], r.get('context_window', '-')))
    lines.append('dropped: %d recipes' % len(merged['dropped']))
    for mid, why in merged['dropped']:
        lines.append('  - %s (%s) — pin into _NEVER_TEMPLATE' % (mid, why))
    if merged['unverified']:
        lines.append('kept but UNVERIFIED this run (transient probe '
                     'failure, not proof of death):')
        for mid, why in merged['unverified']:
            lines.append('  ~ %s (%s)' % (mid, why))
    plaza_delisted = [r['model_id'] for r in merged['kept']
                      if not (set(recipe_wires(r)) & plaza_wires)]
    if plaza_delisted:
        lines.append('kept but delisted from plaza (probe still alive): %s'
                     % ', '.join(sorted(plaza_delisted)))
    if merged['mirror_hints']:
        lines.append('mirror hints (NOT auto-merged):')
        for h in merged['mirror_hints']:
            lines.append('  ? %s' % h)
    if non_chat:
        lines.append('non-chat candidates (report only — defaults are an '
                     'owner decision):')
        for wire, surface, verdict in non_chat:
            lines.append('  * %s [%s] probe=%s' % (wire, surface, verdict))
    if merged['pricing_rows']:
        lines.append('pricing rows staged: %s'
                     % ', '.join(sorted(merged['pricing_rows'])))
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--snapshot', help='saved listV2 response JSON')
    src.add_argument('--cookie',
                     default=os.environ.get('MEITUAN_SSO_COOKIE', ''),
                     help='your-llm-gateway.example.com SSO cookie (or MEITUAN_SSO_COOKIE)')
    ap.add_argument('--verdicts-in', help='reuse saved probe verdicts JSON')
    ap.add_argument('--verdicts-out', help='save probe verdicts JSON')
    ap.add_argument('--write', action='store_true',
                    help='apply to yourprovider.json + lib/pricing/_tables.py')
    args = ap.parse_args(argv)

    if args.snapshot:
        raw_entries = load_snapshot(args.snapshot)
    elif args.cookie:
        raw_entries = fetch_plaza(args.cookie)
    else:
        ap.error('--snapshot or --cookie (MEITUAN_SSO_COOKIE) required')

    candidates: dict[str, dict] = {}
    for raw in raw_entries:
        cand = project_entry(raw)
        if cand:
            candidates[cand['wire']] = cand
    plaza_wires = set(candidates)

    template = _load_template()
    template_wires = {w for r in template['offering_recipes']
                      for w in recipe_wires(r)}

    if args.verdicts_in:
        verdicts = {w: tuple(v) for w, v in
                    json.load(open(args.verdicts_in,
                                   encoding='utf-8')).items()}
    else:
        base_url, key, extra = load_gateway()
        wires = sorted(w for w in (plaza_wires | template_wires)
                       if w in template_wires
                       or classify_surface(candidates[w]) == 'chat')
        print('probing %d wire ids on %s ...' % (len(wires), base_url),
              file=sys.stderr)
        verdicts = probe_wires(wires, base_url, key, extra)
    if args.verdicts_out:
        json.dump(verdicts, open(args.verdicts_out, 'w', encoding='utf-8'),
                  indent=1, ensure_ascii=False)

    merged = merge(template, candidates, verdicts)
    non_chat = [(w, classify_surface(c),
                 verdicts.get(w, ('unprobed (chat-only sync)',))[0])
                for w, c in sorted(candidates.items())
                if classify_surface(c) != 'chat']
    print(_report(merged, non_chat, plaza_wires, template))

    if not args.write:
        print('\n(dry-run — pass --write to apply)')
        return 0

    out = dict(template)
    out['offering_recipes'] = merged['kept'] + merged['added']
    with open(_TEMPLATE_PATH, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write('\n')

    cny_notes = {w: '¥%s/¥%s' % (candidates[w]['cny_in'],
                                 candidates[w]['cny_out'])
                 for w in merged['pricing_rows'] if w in candidates}
    text = open(_PRICING_PATH, encoding='utf-8').read()
    open(_PRICING_PATH, 'w', encoding='utf-8').write(
        update_pricing_text(text, merged['pricing_rows'], cny_notes))
    print('\napplied: template %d recipes, pricing block regenerated'
          % len(out['offering_recipes']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
