"""Immutable owner-bound scientific command originals before model projection.

Entry: install. The shared result pipeline calls capture after post-hooks; the
terminal archive freezes and streams the acknowledged originals. Records own
transport; stage outcomes own review/progression. No replay or completeness of
external experiments is inferred. Unpublished parts grant no source authority.
"""
from __future__ import annotations
import base64
import binascii
import zlib
import hashlib
import json
import threading
from . import records
from .contracts import EvolutionStateError
from .stage_inputs import _put_immutable, _validate

NAMESPACE='evolution.scientific_commands.v1'
PARTS_NAMESPACE='evolution.scientific_command_parts.v1'
PART_CHARS=30000
PART_READ_PAGE=8  # At most 1.44 MiB of worst-case JSON-escaped part text.


def _identity(call_id,digest):
    return hashlib.sha256(json.dumps([call_id,digest],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _encode_part(text):
    encoded=base64.b64encode(zlib.compress(text.encode('utf-8'))).decode('ascii')
    return {'content':encoded,'encoding':'zlib-base64'} if len(encoded)<len(text) else {'content':text}


def _decode_part(part):
    if 'encoding' not in part:return part['content']
    try:
        packed=base64.b64decode(part['content'],validate=True)
        inflater=zlib.decompressobj()
        raw=inflater.decompress(packed,PART_CHARS*4+1)
        if len(raw)>PART_CHARS*4 or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ValueError('compressed part exceeds its bound or contains trailing data')
        text=raw.decode('utf-8')
        if not 1<=len(text)<=PART_CHARS:raise ValueError('decoded part length is invalid')
        return text
    except (ValueError,UnicodeError,zlib.error,binascii.Error) as exc:
        raise EvolutionStateError('scientific command compressed part is invalid') from exc


def _put_part(key,value):
    try:
        _put_immutable(PARTS_NAMESPACE,key,value,kind='scientific command part')
    except EvolutionStateError:
        # Equivalent old raw frames remain immutable during retries after upgrade.
        previous=records.get_record(PARTS_NAMESPACE,key)
        if previous is None:raise
        _validate(previous,'scientific_command_part')
        identity_fields=('schema_version','binding','observation_id','ordinal')
        if (any(previous[name]!=value[name] for name in identity_fields)
                or _decode_part(previous)!=_decode_part(value)):
            raise


class ScientificCommands:
    def __init__(self,binding):
        self.binding=dict(binding)
        _validate(self.binding,'model_attempt_binding')
        self.prefix=hashlib.sha256(binding['task_id'].encode()).hexdigest()+'-'
        self.lock=threading.Lock()
        self.closed=False
        self.active=0
        self.incomplete=False
        self.execution_observed=False

    def _key(self,identity,index=None):
        return records.entity_key(self.prefix+identity+('' if index is None else '-'+str(index)),
                                  self.binding['user_id'],kind='scientific command')

    def _reference_text(self,reference):
        from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
        repo=ToolResultArtifactRepository();cursor='0';seen=set();pieces=[];status=None
        while True:
            if cursor in seen:raise EvolutionStateError('scientific command source cursor repeated')
            seen.add(cursor)
            page=repo.read_range(user_id=self.binding['user_id'],artifact_ref=reference,offset=cursor,limit=65536)
            if (not isinstance(page,dict) or not isinstance(page.get('content'),str)
                    or any(issue.get('reason')!='sourceCoverage' for issue in page.get('unavailable',[])) or page.get('retentionCoverage') in ('partial','incomplete','unavailable')):
                raise EvolutionStateError('scientific command canonical source is unavailable')
            if status is None:status=page.get('executionStatus')
            pieces.append(page['content']);following=page.get('nextCursor')
            if not following:return ''.join(pieces),status
            cursor=following

    def capture(self,carrier,name,call_id,args,canonical,_depth=0):
        try:
            budget=carrier.get('_executionBudget')
            bound=(carrier.get('id')==self.binding['task_id'] or
                   (carrier.get('_executionBudgetControlTaskId')==self.binding['task_id']
                    and budget is not None and budget.root_id==self.binding['task_id']))
            if not bound or carrier.get('_userId')!=self.binding['user_id']:
                raise EvolutionStateError('scientific command carrier changed')
            if _depth>8:raise EvolutionStateError('scientific command tree exceeds canonical depth')
            if name=='run_command':
                if canonical.requires_reference():
                    from lib.tools.result_value import persist_result,capture_result
                    reference=persist_result(canonical,user_id=self.binding['user_id'])
                    text,_=self._reference_text(reference)
                    canonical=capture_result(text,execution_status=canonical.execution_status)
                self._capture(call_id,canonical)
            # Composite program values never create command observations;
            # only framework-owned child identities and originals are visited.
            for child in canonical.children:
                value=child.canonical
                if value is None:
                    if child.name!='run_command':continue
                    from lib.tools.result_value import capture_result
                    text,status=self._reference_text(child.artifact_ref)
                    if status is None:raise EvolutionStateError('scientific command child lacks execution provenance')
                    value=capture_result(text,execution_status=status)
                self.capture(carrier,child.name,child.call_id,None,value,_depth+1)
        except Exception:
            with self.lock:self.incomplete=True
            raise

    def _capture(self,call_id,canonical):
        text=canonical.text()
        from .tool_evidence import SCIENTIFIC_MARKERS
        from lib.project_mod.file_read_probe import FILE_READ_OBSERVATION_KEY
        if not isinstance(call_id,str) or not call_id or not any(
                marker in text for marker in (*SCIENTIFIC_MARKERS, '"' + FILE_READ_OBSERVATION_KEY + '"')):
            return
        with self.lock:
            if self.closed:
                self.incomplete=True
                raise EvolutionStateError('scientific command capture already closed')
            from .tool_evidence import background_execution_observed
            self.execution_observed |= background_execution_observed([{'content':text,'error':canonical.execution_status!='ok'}])
            self.active+=1
        try:
            digest=hashlib.sha256()
            for offset in range(0,len(text),PART_CHARS):digest.update(text[offset:offset+PART_CHARS].encode())
            sha=digest.hexdigest();identity=_identity(call_id,sha)
            parts=(len(text)+PART_CHARS-1)//PART_CHARS
            for index in range(parts):
                value={'schema_version':1,'binding':self.binding,'observation_id':identity,
                       'ordinal':index,**_encode_part(text[index*PART_CHARS:(index+1)*PART_CHARS])}
                _validate(value,'scientific_command_part')
                _put_part(self._key(identity,index),value)
            manifest={'schema_version':1,'binding':self.binding,'observation_id':identity,
                      'tool_call_id':call_id,'content_sha256':sha,'content_chars':len(text),
                      'content_parts':parts,'status':'done' if canonical.execution_status=='ok' else 'error'}
            _validate(manifest,'scientific_command')
            with self.lock:
                if self.closed:raise EvolutionStateError('scientific command capture closed during persistence')
                _put_immutable(NAMESPACE,self._key(identity),manifest,kind='scientific command')
        except Exception:
            with self.lock:self.incomplete=True
            raise
        finally:
            with self.lock:self.active-=1

    def _manifests(self):
        # Freeze this local capture scope once; late producers cannot change the
        # terminal stream between descriptor calculation and immutable writes.
        with self.lock:
            self.closed=True
            self.incomplete |= bool(self.active)
        after=''
        while True:
            rows=records.list_record_page(NAMESPACE,self.binding['user_id'],kind='scientific command',
                limit=records.MAX_LIST_LIMIT,after_key=after,entity_prefix=self.prefix)
            for row in rows:
                value=row['value'];_validate(value,'scientific_command')
                identity=_identity(value['tool_call_id'],value['content_sha256'])
                if (value['binding']!=self.binding or value['observation_id']!=identity
                        or row['key']!=self._key(identity)):
                    raise EvolutionStateError('scientific command source identity changed')
                yield value,identity
            if len(rows)<records.MAX_LIST_LIMIT:return
            after=rows[-1]['key']

    def call_ids(self):
        for value,_ in self._manifests():yield value['tool_call_id']

    def _parts(self,value,identity):
        from lib.storage.errors import StorageError
        parts={};after='';limit=PART_READ_PAGE
        while True:
            try:
                rows=records.list_record_page(PARTS_NAMESPACE,self.binding['user_id'],
                    kind='scientific command parts',limit=limit,after_key=after,
                    entity_prefix=self.prefix+identity+'-')
            except StorageError as exc:
                if limit>1 and records.is_record_page_resource_refusal(exc):
                    limit=max(1,limit//2)
                    continue
                raise
            for row in rows:
                part=row['value'];_validate(part,'scientific_command_part')
                index=part['ordinal']
                if (part['binding']!=self.binding or part['observation_id']!=identity
                        or row['key']!=self._key(identity,index)
                        or index>=value['content_parts'] or index in parts):
                    raise EvolutionStateError('scientific command part identity changed')
                parts[index]=_decode_part(part)
            if len(rows)<limit:break
            after=rows[-1]['key']
        if len(parts)!=value['content_parts']:
            raise EvolutionStateError('scientific command part is unavailable')
        # Storage keys sort lexically (1, 10, 100, 11...), not by ordinal.
        return [parts[index] for index in range(value['content_parts'])]

    def messages(self):
        for value,identity in self._manifests():
            pieces=self._parts(value,identity);digest=hashlib.sha256();count=0
            for text in pieces:
                digest.update(text.encode());count+=len(text)
            if count!=value['content_chars'] or digest.hexdigest()!=value['content_sha256']:
                raise EvolutionStateError('scientific command content changed')
            yield {'tool_call_id':value['tool_call_id'],'name':'run_command',
                   'source':'acknowledged_scientific_command_original','content':''.join(pieces),
                   'error':value['status']!='done'}


def install(task):
    binding={'schema_version':1,**task['_evolutionCheckpointBinding']}
    capture=ScientificCommands(binding)
    task['_evolutionScientificCommandCapture']=capture.capture
    task['_evolutionScientificCommandRead']=capture.messages
    task['_evolutionScientificCommandCallIds']=capture.call_ids
    task['_evolutionScientificCommandIncomplete']=lambda:capture.incomplete
    task['_evolutionScientificCommandExecuted']=lambda:capture.execution_observed
