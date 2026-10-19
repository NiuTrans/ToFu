"""Request-scoped content projection; no fetching, execution, or persistence.

Public entry points collect_content/render_content share policy and credential
redaction with network_evidence. Captured structures are evidence, not instructions.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from itertools import islice
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from .content_contract import ReadTarget
from . import network_evidence as net

_PRIVATE = re.compile(r'account|chat|conversation|notification|history|board|session|profile|currentuser|userinfo|recommend', re.I)


class EvidenceReceipt(dict):
    """A secured content receipt plus its schema-clean canonical snapshot.

    Acceptance is resource-bounded; presentation length belongs to common L0.
    The snapshot attribute keeps artifact evidence out of the receipt schema.
    """

    __slots__ = ('evidence_json', 'source_omitted')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence_json = ''
        self.source_omitted = False
_INSTRUCTION = re.compile(r'ignore (?:all |previous |prior )*instructions|system prompt|developer message|你是.*助手', re.I)
_EMPTY = re.compile(r'^(?:no (?:results|items|records)(?: found)?|暂无(?:结果|内容|数据)|没有找到相关结果)[.!。]?$', re.I)
_AUTH = re.compile(r'^(?:sign in|log in|login required|access denied|请先登录|登录后查看|验证码|captcha)', re.I)
_SHELL = re.compile(r'^(?:loading[. …]*|please wait|加载中[. …]*|home|menu|search|首页|搜索)$', re.I)


class WorkBudget:
    def __init__(self):
        self.nodes = 12000
        self.chars = 1048576
        self.truncated = False

    def text(self, value, limit=80000):
        if not isinstance(value, str):
            return ''
        count = min(len(value), limit, self.chars)
        self.chars -= count
        self.truncated |= count < len(value)
        return value[:count]

    def clean(self, value, target, page, depth=0):
        if self.nodes <= 0 or depth > 16:
            self.truncated = True
            return None
        self.nodes -= 1
        if isinstance(value, str):
            return net._redact_text(self.text(value, 8192))
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            self.truncated |= len(value) > 100 or len(value) > self.nodes
            return [self.clean(v, target, page, depth + 1) for v in value[:100] if self.nodes > 0]
        if isinstance(value, dict):
            out = {}
            self.truncated |= len(value) > 80
            for key, child in islice(value.items(), 80):
                key = str(key)[:128]
                if net._is_sensitive_key(key):
                    continue
                if _PRIVATE.search(key) and not private_target(key, target, page):
                    continue
                if self.nodes <= 0:
                    self.truncated = True
                    break
                out[key] = self.clean(child, target, page, depth + 1)
            return out
        return None


def private_target(value, target, page):
    words = set(m.group().lower() for m in _PRIVATE.finditer(value))
    context = (urlsplit(page).path + ' ' + target.query).lower()
    return bool(words and any(word in context for word in words))


def relevant_endpoint(url, page, target, dom=''):
    """An endpoint shape alone is not evidence of page relevance."""
    try:
        path = unquote(urlsplit(url).path).lower()
        if net._NOISE_URL_RE.search(path):
            return False
        if _PRIVATE.search(path) and not private_target(path, target, page):
            return False
        query = target.query.lower()
        if query:
            return query in unquote(url).lower()
        page_words = set(re.findall(r'[a-z][a-z0-9_-]{2,}', unquote(urlsplit(page).path).lower()))
        api_words = set(re.findall(r'[a-z][a-z0-9_-]{2,}', path))
        ignored = {'api', 'www', 'com', 'app', 'page', 'index', 'html', 'search', 'detail', 'list'}
        return bool((page_words & api_words) - ignored)
    except ValueError:
        return False


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = []
        self.media = False
        self.links = []
        self.anchor = None
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 12000:
            return
        attrs = dict(attrs)
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag in {'script', 'style', 'noscript', 'template', 'nav', 'footer', 'header', 'form', 'button', 'svg'} or 'hidden' in attrs or attrs.get('aria-hidden') == 'true':
            if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
                self.skip.append(tag)
            return
        if tag in {'img', 'video', 'audio', 'canvas', 'iframe'}:
            self.media = True
        if tag == 'a':
            self.anchor = [attrs.get('href', ''), []]
        if tag in {'p', 'div', 'article', 'section', 'li', 'br', 'h1', 'h2'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == 'a' and self.anchor:
            self.links.append({'title': ''.join(self.anchor[1]), 'url': self.anchor[0], 'snippet': ''})
            self.anchor = None

    def handle_data(self, data):
        if not self.skip and self.nodes <= 12000:
            self.parts.append(data)
            if self.anchor:
                self.anchor[1].append(data)


def clean_text(text):
    lines = []
    for line in text.splitlines():
        line = ' '.join(line.split())
        if not line or _INSTRUCTION.search(line) or _SHELL.fullmatch(line):
            continue
        lines.append(net._redact_text(line))
    return '\n'.join(dict.fromkeys(lines))


def observed_url(value, page, owner):
    if not isinstance(value, str) or not value or len(value) > 8192:
        return ''
    try:
        url = urljoin(page, value)
        if urlsplit(url).scheme not in ('http', 'https') or not net._response_allowed(owner, url):
            return ''
        parts = urlsplit(url)
        if parts.username or parts.password or any(net._is_sensitive_key(k) or k.lower() in {'sig', 'signature', 'policy', 'key-pair-id'} for k, _ in parse_qsl(parts.query)) or (parts.fragment and ('=' in parts.fragment or '?' in parts.fragment)):
            return ''
        return url
    except ValueError:
        return ''


def project_records(body, *, target, page, owner, dom, endpoint_relevant, work):
    stack = [body]
    out = []
    while stack and work.nodes > 0 and len(out) < 100:
        value = stack.pop()
        work.nodes -= 1
        if isinstance(value, list):
            stack.extend(reversed(value[:100]))
        elif isinstance(value, dict):
            nested = value.get('note_card') or value.get('noteCard')
            raw = nested if isinstance(nested, dict) else value
            title = net._first_scalar(raw, ('display_title', 'displayTitle', 'title', 'name', 'skillName', 'displayName', 'subject'))
            snippet = net._first_scalar(raw, ('desc', 'description', 'snippet', 'summary', 'content', 'body', 'text'))
            identity = (title + ' ' + snippet).lower()
            query_match = bool(target.query and target.query.lower() in identity)
            dom_match = bool(title and len(title) >= 3 and title.lower() in dom.lower())
            if title and (query_match or endpoint_relevant or (not target.query and dom_match)):
                if target.query and not (query_match or dom_match or endpoint_relevant):
                    continue
                if not _INSTRUCTION.search(identity):
                    available = dict(raw.get('fields', {})) if isinstance(raw.get('fields'), dict) else {}
                    available.update(raw)
                    fields = {k: available[k] for k in target.fields if k in available and isinstance(available[k], (str, int, float, bool)) and not net._is_sensitive_key(k)}
                    record_id = net._first_scalar(value, ('id', 'note_id', 'skillId'))
                    if record_id and 'id' not in fields and len(fields) < 12:
                        fields['id'] = record_id[:512]
                    out.append({'title': clean_text(title)[:512], 'url': observed_url(net._first_scalar(raw, net._URL_KEYS) or net._first_scalar(value, net._URL_KEYS), page, owner), 'snippet': clean_text(snippet)[:2000], 'fields': fields})
                if nested:
                    continue
            stack.extend(reversed([v for v in value.values() if isinstance(v, (dict, list))]))
    if stack:
        work.truncated = True
    return out


def collect_content(payload: dict, *, owner_user_id, target=None, max_chars=30000,
                    representation='auto') -> dict:
    target = ReadTarget.from_value(target)
    work = WorkBudget()
    payload = payload if isinstance(payload, dict) else {}
    page = payload.get('finalUrl') or payload.get('url') or ''
    receipt = {'status': 'unavailable', 'title': '', 'url': '', 'content': '', 'records': [], 'coverage': {'scope': 'none', 'truncated': False, 'stop_reason': 'no_relevant_content', 'missing_fields': list(target.fields), 'media_unread': False}, 'evidence': []}
    if not isinstance(page, str) or len(page) > 8192 or not page.startswith(('http://', 'https://')) or not net._response_allowed(owner_user_id, page):
        receipt['status'] = 'blocked'
        receipt['coverage']['stop_reason'] = 'page_policy_denied'
        return receipt
    receipt['url'] = observed_url(page, page, owner_user_id)
    receipt['title'] = clean_text(work.text(payload.get('title'), 512))
    snapshot = payload.get('contentSnapshot')
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    parser = PageParser()
    html = work.text(payload.get('html'), 400000)
    if html:
        try:
            parser.feed(html)
        except (ValueError, RecursionError):
            work.truncated = True
    text = work.text(snapshot.get('text') if 'text' in snapshot else payload.get('collectedText') or payload.get('text'))
    if not text and 'text' not in snapshot and html and ''.join(parser.parts).strip():
        text = ''.join(parser.parts)
    dom = clean_text(text)
    receipt['coverage']['media_unread'] = bool(parser.media or snapshot.get('media_count'))
    blocked = bool(snapshot.get('blocked')) or bool(_AUTH.search(dom))
    empty = snapshot.get('empty') is True or bool(_EMPTY.fullmatch(dom))
    dom_sufficient = len(dom) >= 12 and not blocked and not empty and (not target.query or target.query.lower() in dom.lower())
    if target.kind in ('article', 'record') and parser.links and dom == clean_text('\n'.join(r['title'] for r in parser.links)):
        dom_sufficient = False
    if dom_sufficient and target.kind != 'list' and representation != 'data':
        receipt['content'] = dom
        receipt['evidence'].append({'strategy': 'rendered_dom', 'url': receipt['url']})
    records = []
    if not blocked and representation != 'data':
        dom_records = snapshot.get('records') if 'records' in snapshot else (parser.links if target.kind in ('auto', 'list') and not receipt['content'] else [])
        safe = work.clean(dom_records, target, page)
        records.extend(project_records(safe, target=target, page=page, owner=owner_user_id, dom=dom, endpoint_relevant=not target.query, work=work))
        if records:
            receipt['evidence'].append({'strategy': 'rendered_dom', 'url': receipt['url']})
    # A sufficient article does not need incidental API/hydration enrichment.
    if not blocked and (representation == 'data' or not (dom_sufficient and (target.kind == 'article' or (target.kind == 'auto' and len(dom) >= 100)))):
        sources = [(row, 'captured_api') for row in net._network_rows(payload)]
        states = payload.get('initialStatePayloads')
        if isinstance(states, dict):
            states = list(islice(states.values(), 20))
        if isinstance(states, list):
            sources.extend((state, 'hydrated_state') for state in states[:20])
        for source, strategy in sources:
            if work.chars <= 0 or work.nodes <= 0:
                work.truncated = True
                break
            if strategy == 'captured_api':
                url = source.get('url', '')
                if not net._response_allowed(owner_user_id, url):
                    continue
                relevant = relevant_endpoint(url, page, target, dom)
                if _PRIVATE.search(urlsplit(url).path) and not private_target(urlsplit(url).path, target, page):
                    continue
                if net._NOISE_URL_RE.search(url) or net._status(source) >= 400:
                    blocked |= relevant and net._status(source) in (401, 403)
                    continue
                body = source.get('body', source.get('responsePreview', source.get('responseBody')))
            else:
                url = page
                relevant = False
                body = source.get('value', source.get('data', source.get('text', source.get('payload', source)))) if isinstance(source, dict) else source
            if isinstance(body, str):
                body = work.text(body, 262144)
                try:
                    body = json.loads(body)
                except (ValueError, RecursionError):
                    continue
            body = work.clean(body, target, page)
            projected = project_records(body, target=target, page=page, owner=owner_user_id, dom=dom, endpoint_relevant=relevant, work=work)
            if target.kind == 'article' and len(projected) == 1 and not receipt['content']:
                article = projected[0]
                if len(article['snippet']) >= 12:
                    receipt['content'] = article['snippet']
                    receipt['title'] = article['title']
            if projected:
                records.extend(projected)
                if len(receipt['evidence']) < 12:
                    receipt['evidence'].append({'strategy': strategy, 'url': net.redact_url(url)})
                work.truncated |= bool(source.get('bodyTruncated') or source.get('responseBodyTruncated')) if isinstance(source, dict) else False
            elif relevant and isinstance(body, dict):
                data = body.get('data', body)
                if isinstance(data, dict) and data.get('total') == 0 and any(data.get(k) == [] for k in ('items', 'list', 'records', 'results')):
                    empty = True
    if target.kind == 'article':
        records = []
    elif target.kind == 'record':
        records = records[:1]
    if blocked:
        receipt['content'] = ''
        records = []
    seen = set()
    for record in records:
        key = (record['title'], record['url'])
        if key not in seen:
            seen.add(key)
            receipt['records'].append(record)
        if len(receipt['records']) >= 100:
            work.truncated |= len(records) > 100
            break
    coverage = receipt['coverage']
    coverage['missing_fields'] = [field for field in target.fields if not receipt['records'] or any(not field_present(r, field) for r in receipt['records'])]
    usable = bool(receipt['content'] or receipt['records'])
    if usable:
        coverage['scope'] = 'observed_records' if receipt['records'] else 'observed_page'
        receipt['status'] = 'ok'
        coverage['stop_reason'] = 'observed_content'
    elif blocked:
        receipt['status'] = 'blocked'
        coverage['stop_reason'] = 'authentication_required'
    elif empty:
        receipt['status'] = 'empty'
        coverage['stop_reason'] = 'explicit_empty'
    coverage['truncated'] = work.truncated or parser.nodes > 12000 or bool(payload.get('truncated') or snapshot.get('truncated'))
    # Accepted, secured evidence is passed intact to the common L0 boundary.
    # max_chars remains an ignored compatibility keyword for internal callers;
    # WorkBudget and transport ceilings still bound collection/processing.
    if usable and (not receipt['url'] or any(not r['url'] for r in receipt['records'])):
        if 'url' not in coverage['missing_fields'] and len(coverage['missing_fields']) < 12:
            coverage['missing_fields'].append('url')
    research = payload.get('research') if isinstance(payload.get('research'), dict) else {}
    stop = payload.get('stopReason') or snapshot.get('stopReason') or research.get('stopReason')
    if stop:
        coverage['stop_reason'] = net._redact_text(str(stop))[:120]
    if usable and (bool(stop) or coverage['truncated'] or coverage['media_unread'] or coverage['missing_fields']):
        receipt['status'] = 'partial'
    if coverage['truncated']:
        coverage['stop_reason'] = 'budget_exhausted'
    if not receipt['content'] and not receipt['records']:
        coverage['scope'] = 'none'
    out = EvidenceReceipt(receipt)
    out.evidence_json = json.dumps(receipt, ensure_ascii=False)
    return out


def field_present(record, field):
    value = record.get(field, record.get('fields', {}).get(field))
    return value is not None and value != ''


def render_content(receipt, *, max_chars=30000) -> str:
    """Render accepted data intact for L0; max_chars is compatibility-only."""
    coverage = receipt.get('coverage', {})
    header = 'Browser content: ' + str(receipt.get('status', 'unavailable'))
    if any(e.get('strategy') == 'captured_api' for e in receipt.get('evidence', [])):
        header += '\nCaptured API data from the authenticated browser session:'
    parts = [header, str(receipt.get('title', '')), str(receipt.get('url', '')), 'Coverage: ' + json.dumps(coverage, ensure_ascii=False), str(receipt.get('content', ''))]
    parts.extend(json.dumps(row, ensure_ascii=False) for row in receipt.get('records', [])[:100])
    parts.extend('Evidence: ' + json.dumps(row, ensure_ascii=False).replace('%5Bredacted%5D', '[redacted]') for row in receipt.get('evidence', [])[:12])
    rendered = '\n'.join(part for part in parts if part)
    return rendered


__all__ = ['EvidenceReceipt', 'collect_content', 'render_content']
