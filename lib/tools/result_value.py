"""Runtime-only canonical evidence. Display strings never become child sources.

sourceCoverage describes upstream completeness (unknown absent assertion).
retentionCoverage describes retained received evidence, independently of upstream.
"""
from __future__ import annotations
import json
import uuid
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from lib.log import get_logger
from lib.tool_results.tool_result_query_contract import MEDIA_TYPE as MANIFEST_MEDIA_TYPE
INLINE_LIMIT = 1024 * 1024

# Below the 16 MiB repository row limit, with headroom for manifest/quotas.
_ARTIFACT_TEXT_CHUNK_BYTES = 4 * 1024 * 1024
logger = get_logger(__name__)

class TrustedResultText(str):
    def __new__(cls, text, *, canonical=None, envelope=False, source_omitted=False,
                projection_normalized=False):
        obj = super().__new__(cls, text)
        if source_omitted is True and isinstance(canonical, CapturedToolResult):
            canonical = replace(canonical, source_omitted=True)
        if projection_normalized is True and isinstance(canonical, CapturedToolResult):
            canonical = replace(canonical, projection_normalized=True)
        obj.canonical, obj.envelope = canonical, envelope
        return obj

class CanonicalReceipt(dict):
    """JSON serializes only public receipt fields, never the native descriptor."""
    def __init__(self, *args, canonical=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.canonical = canonical

        self.source_omitted = getattr(canonical, 'source_omitted', False) is True
@dataclass(frozen=True)
class ResultPart:
    kind: str
    text: str = ''
    reference: str = ''
    mime_type: str = ''
    metadata: tuple = ()
    def manifest(self):
        out = {'kind': self.kind}
        if self.kind == 'artifact':
            out['artifactRef'] = self.reference
        elif self.kind == 'media':
            out.update(reference=self.reference, mimeType=self.mime_type)
        else:
            out['text'] = self.text
        out.update(dict(self.metadata))
        return out

@dataclass(frozen=True)
class ResultChild:
    name: str
    call_id: str
    artifact_ref: str = ''
    canonical: CapturedToolResult | None = None

@dataclass(frozen=True)
class CapturedToolResult:
    parts: tuple[ResultPart, ...]
    children: tuple[ResultChild, ...] = ()
    result_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    execution_status: str = 'ok'
    source_coverage: str = 'unknown'
    retention_coverage: str = 'complete'
    artifact_ref: str = field(default='', compare=False)
    artifact_owner: int = field(default=0, compare=False)
    # Runtime projection policy, never serialized into a source manifest.
    # Program children are retained evidence, not omitted answer material.
    private_children: bool = field(default=False, compare=False)

    # Host-only facts; never decode from application JSON/metadata.
    source_omitted: bool = False
    # Lossless presentation normalization may need storage, never a read prompt.
    projection_normalized: bool = False
    def text(self):
        return '\n'.join([p.text for p in self.parts if p.kind in ('text', 'json')]
                         + [c.canonical.text() for c in self.children if c.canonical is not None])
    def requires_reference(self):
        return (any(p.kind in ('artifact', 'media') for p in self.parts)
                or any(c.canonical is None or c.canonical.requires_reference()
                       for c in self.children))
    def media_references(self, _depth=0):
        """Retained ``tofu-media:`` refs across this inline result tree.

        Only INLINE children are walked: a spilled child (``canonical is
        None``) keeps its media behind its artifact ref, resolvable through
        ``read_tool_artifact`` descriptors instead. Depth stays inside the
        manifest contract's scan bound.
        """
        if _depth > 8:
            return []
        refs = [p.reference for p in self.parts
                if p.kind == 'media' and isinstance(p.reference, str)
                and p.reference.startswith('tofu-media:')]
        for child in self.children:
            if child.canonical is not None:
                refs.extend(child.canonical.media_references(_depth + 1))
        return refs
    def retained_size(self):
        """Bound runtime bodies without serializing inline children as protocol."""
        own = replace(self, children=())
        return len(json.dumps(own.manifest(), ensure_ascii=False).encode('utf-8')) + sum(
            len(c.name.encode('utf-8')) + len(c.call_id.encode('utf-8')) + 256
            + (c.canonical.retained_size() if c.canonical is not None else 0)
            for c in self.children)
    def manifest(self):
        if any(c.canonical is not None for c in self.children):
            raise ValueError('Inline children must be published before manifest serialization')
        return {'resultId': self.result_id, 'executionStatus': self.execution_status,
                'sourceCoverage': self.source_coverage, 'retentionCoverage': self.retention_coverage,
                'parts': [p.manifest() for p in self.parts],
                'children': [{'name': c.name, 'callId': c.call_id, 'artifactRef': c.artifact_ref} for c in self.children]}


def tool_result_failure_verdict(content):
    """Read host/protocol execution metadata, never infer failure from payload JSON.

    Only the outer operation owns its verdict. A completed batch can retain
    failed children, and partial source/retention coverage is not execution failure.
    """
    value = (content if isinstance(content, CapturedToolResult)
             else getattr(content, 'canonical', None))
    if isinstance(value, CapturedToolResult):
        return {
            'error': 'error', 'failed': 'error', 'cancelled': 'aborted',
            'aborted': 'aborted', 'rejected': 'rejected',
        }.get(value.execution_status)
    from lib.mcp.result_content import MCPToolResult
    if isinstance(content, MCPToolResult) and content.is_error:
        return 'error'
    return None


def capture_result(content: Any, *, execution_status=None, source_coverage=None):
    existing = content if isinstance(content, CapturedToolResult) else getattr(content, 'canonical', None)
    if isinstance(existing, CapturedToolResult):
        changes = {}
        if execution_status is not None: changes['execution_status'] = execution_status
        if source_coverage is not None: changes['source_coverage'] = source_coverage
        changes = {key: value for key, value in changes.items()
                   if value != getattr(existing, key)}
        return replace(existing, artifact_ref='', artifact_owner=0, **changes) if changes else existing
    from lib.mcp.result_content import MCPToolResult
    status, coverage = execution_status or 'ok', source_coverage or 'unknown'
    if isinstance(content, MCPToolResult):
        parts = []
        refs = {m.get('sourceOrdinal'): m for m in content.media_refs if isinstance(m, dict)}
        blocks = content.content_blocks
        for ordinal, block in enumerate(blocks):
            meta = {'sourceOrdinal': block.get('sourceOrdinal', ordinal)}
            for key in ('uri', 'omittedCount', 'status', 'reason'):
                if key in block:
                    meta[key] = block[key]
            if isinstance(block.get('text'), str):
                parts.append(ResultPart('text', block['text'], metadata=tuple(meta.items())))
            else:
                media = refs.pop(meta['sourceOrdinal'], {})
                meta.update({k: media[k] for k in ('view','sha256','byteSize','caption','status','reason') if k in media})
                ref = media.get('mediaRef') or media.get('reference') or ''
                if not ref: meta.update(status='unavailable', reason=block.get('reason') or 'unsupported_or_unretained_part')
                parts.append(ResultPart('media', reference=ref, mime_type=media.get('mimeType',''), metadata=tuple(meta.items())))
        structured = content.structured_json
        if not blocks and not content.retention_error and structured is None:
            text = content.document_text if content.document_text is not None else str(content)
            if text: parts.append(ResultPart('text', text))
        if structured is not None:
            parts.append(ResultPart('json', structured if isinstance(structured,str) else json.dumps(structured, ensure_ascii=False)))
        for media in refs.values():
            meta = tuple((k,media[k]) for k in ('sourceOrdinal','view','sha256','byteSize','caption','status','reason') if k in media)
            parts.append(ResultPart('media', reference=media.get('mediaRef') or media.get('reference',''), mime_type=media.get('mimeType',''), metadata=meta))
        if content.retention_error and not parts:
            parts.append(ResultPart('media', metadata=(('status','unavailable'),('reason','document_retention_limit'))))
        return CapturedToolResult(tuple(parts), execution_status=execution_status or ('error' if content.is_error else 'ok'), source_coverage=coverage,
            retention_coverage='partial' if content.retention_error or any(dict(p.metadata).get('status')=='unavailable' for p in parts) else 'complete')
    if isinstance(content, dict) and (content.get('__screenshot__') or content.get('__batch_images__')):
        parts = [ResultPart('text', str(content.get('_text_fallback') or content.get('_text_content') or '[Image captured]'))]
        parts += [ResultPart('media', reference=r, mime_type='image/*') for r in content.get('_modelReplaySources',()) if isinstance(r,str)]
        return CapturedToolResult(tuple(parts), execution_status=status, source_coverage=coverage, retention_coverage='complete' if len(parts)>1 else 'unavailable')
    text = content if isinstance(content,str) else json.dumps(content,ensure_ascii=False)
    try:
        json.loads(text)
        kind='json'
    except (ValueError,TypeError): kind='text'
    return CapturedToolResult((ResultPart(kind,text),), execution_status=status, source_coverage=coverage)

def posthook_result(content, hook):
    """Redact every canonical text part and the independent producer projection."""
    value = capture_result(content)
    def transform(p):
        metadata = tuple((key, str(hook(value)) if key in ('caption', 'uri', 'reason')
                          and isinstance(value, str) else value)
                         for key, value in p.metadata)
        p = replace(p, metadata=metadata)
        if p.kind not in ('text', 'json'): return p
        text = str(hook(p.text))
        kind = p.kind
        if kind == 'json':
            try: json.loads(text)
            except (ValueError, TypeError): kind = 'text'
        return replace(p, text=text, kind=kind)
    parts = tuple(transform(p) for p in value.parts)
    children = tuple(replace(c, canonical=capture_result(posthook_result(c.canonical, hook)))
                     if c.canonical is not None else c for c in value.children)
    projection = (hook(str(content)) if isinstance(content, TrustedResultText)
                  and not content.envelope else None)
    if parts == value.parts and children == value.children and (
            projection is None or projection == str(content)):
        return content
    # A producer envelope that is also its complete source can remain an
    # envelope after redaction. Budgeted views with independent raw sources
    # cannot: their sources may themselves contain lookalike JSON markers.
    envelope = False
    if (isinstance(content, TrustedResultText) and content.envelope
            and not value.children and len(value.parts) == 1
            and value.parts[0].text == str(content)
            and value.parts[0].kind == parts[0].kind == 'json'):
        before, after = json.loads(value.parts[0].text), json.loads(parts[0].text)
        from lib.tools.result_envelope import TOOL_RESULT_CONTRACT_VERSION
        envelope = (isinstance(before, dict) and isinstance(after, dict)
                    and before.get('contractVersion') == after.get('contractVersion')
                    == TOOL_RESULT_CONTRACT_VERSION
                    and before.get('status') == after.get('status'))
    value = replace(value, parts=parts, children=children, artifact_ref='', artifact_owner=0)
    if projection is None:
        projection = value.text()
    return TrustedResultText(projection, canonical=value, envelope=envelope)

def _text_chunks(text: str):
    """Slice on code-point boundaries without splitting a UTF-8 byte sequence."""
    start = size = 0
    for index, char in enumerate(text):
        width = len(char.encode('utf-8'))
        if size + width > _ARTIFACT_TEXT_CHUNK_BYTES and index > start:
            yield text[start:index]
            start, size = index, 0
        size += width
    if start < len(text):
        yield text[start:]


def persist_result(value, *, user_id, now_ms=None):
    if value.artifact_ref and value.artifact_owner == int(user_id or 0): return value.artifact_ref
    if int(user_id or 0)<=0: raise ValueError('retention_owner_required')
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo=ToolResultArtifactRepository()
    parts=[]
    for p in value.parts:
        if p.kind in ('text', 'json') and len(p.text.encode('utf-8')) > 65536:
            split_json = (p.kind == 'json' and
                          len(p.text.encode('utf-8')) > _ARTIFACT_TEXT_CHUNK_BYTES)
            for chunk in _text_chunks(p.text):
                # JSON selectors are valid only for a whole JSON document.
                # A split JSON source is searchable/readable text, not a
                # syntactically valid standalone JSON part.
                media_type = ('text/plain' if split_json else
                              'application/json' if p.kind == 'json' else 'text/plain')
                stored = repo.put(user_id=int(user_id), content=chunk,
                                  media_type=media_type, now_ms=now_ms or None)
                if not stored.get('artifactRef'): raise RuntimeError('retention_failed')
                parts.append(ResultPart('artifact', reference=stored['artifactRef'],
                                        metadata=p.metadata))
        else:
            parts.append(p)
    children = tuple(ResultChild(c.name, c.call_id,
                     persist_result(c.canonical, user_id=user_id, now_ms=now_ms)
                     if c.canonical is not None else c.artifact_ref)
                     for c in value.children)
    compact=replace(value,parts=tuple(parts),children=children)
    stored=repo.put(user_id=int(user_id),content=json.dumps(compact.manifest(),ensure_ascii=False),media_type=MANIFEST_MEDIA_TYPE,now_ms=now_ms or None)
    ref=str((stored or {}).get('artifactRef') or '')
    if not ref: raise RuntimeError('retention_failed')
    # Memoize publication identity only; readers holding the capture keep its bytes.
    object.__setattr__(value,'artifact_ref',ref)
    object.__setattr__(value,'artifact_owner',int(user_id))
    return ref

class CanonicalBatchSink:
    def __init__(self,user_id,projection_sink=None):
        self.user_id=user_id
        self.results={}
        self.projection_sink=projection_sink
        self.remaining=INLINE_LIMIT
        self.lock=threading.Lock()
    def capture(self,call_id,content):
        value=capture_result(content)
        if self.projection_sink is not None: self.projection_sink.capture(call_id,value)
        with self.lock:
            if call_id in self.results: return
            size=value.retained_size()
            receipt=CanonicalReceipt(resultId=value.result_id)

            receipt.source_omitted = value.source_omitted
            owner = getattr(self.projection_sink, '_owner', None)
            available = min(self.remaining, owner._canonical_remaining) if owner is not None else self.remaining
            if size<=available:
                self.remaining-=size
                if owner is not None: owner._canonical_remaining-=size
                receipt.canonical=value
            else:
                try: receipt['canonicalRef']=persist_result(value,user_id=self.user_id)
                except Exception as exc:
                    logger.warning('Canonical batch retention unavailable (%s)', type(exc).__name__)
                    receipt['retentionError']='canonical_retention_unavailable'
            self.results[call_id]=receipt
    def result(self,call_id):
        original=self.results.get(call_id)
        value=CanonicalReceipt(original or {},canonical=getattr(original,'canonical',None))

        value.source_omitted = getattr(original, 'source_omitted', False) is True
        if self.projection_sink is not None: value.update(self.projection_sink.result(call_id) or {})
        return value or None
    def finish(self):
        if self.projection_sink is not None: self.projection_sink.finish()

_NO_PROGRAM_VALUE = object()

def compose_result(text,receipts,*,program_value=_NO_PROGRAM_VALUE,
                   execution_status='ok',private_children=False):
    parts=[]; children=[]; failed=False; source_omitted=False
    for r in receipts:
        value=getattr(r,'canonical',None)
        source_omitted |= isinstance(r, CanonicalReceipt) and (
            r.get('output_truncated') is True or r.source_omitted is True or
            getattr(value, 'source_omitted', False) is True)
        if value is not None:
            children.append(ResultChild(r['name'], r['call_id'], canonical=value))
            failed |= value.retention_coverage!='complete'
        elif r.get('canonicalRef'): children.append(ResultChild(r['name'],r['call_id'],r['canonicalRef']))
        failed |= bool(r.get('retentionError'))
    if program_value is not _NO_PROGRAM_VALUE:
        # The program's returned type is authoritative, including null and
        # strings that happen to look like JSON. Ordinary text capture guesses
        # a part kind and would lose those distinctions.
        parts.append(ResultPart('json', json.dumps(program_value, ensure_ascii=False)))
    value=CapturedToolResult(tuple(parts),tuple(children),execution_status=execution_status,
        retention_coverage='partial' if failed else 'complete',
        source_omitted=not private_children and source_omitted,
        private_children=private_children)
    return TrustedResultText(text,canonical=value)
