"""Native tool callbacks over the canonical application dispatch pipeline.

Entry: NativeGateway. Explicit task/principal, prepared catalog/config, model
round source and owned executor are borrowed. Existing parser, approval gate,
hooks, resolver, result budgeting and settlement remain their current owners.
No direct handler lookup, second executor, model loop or storage path is created.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from contextvars import copy_context
from dataclasses import dataclass
import json
import threading
import time
import uuid

from lib.agent_core.native_process import ApprovalRequest, ApprovalPolicy, ToolRequest
from lib.identity import PrincipalContext

from lib.log import get_logger

logger = get_logger(__name__)


def compile_native_tool_catalog(task: dict, tool_list: list, *, permissions=None) -> dict:
    """Compile prepared function tools using canonical task safety partitions."""
    encoded, _ = _serialize_native_tool_catalog(task, tool_list, permissions=permissions)
    return json.loads(encoded)


def _serialize_native_tool_catalog(task: dict, tool_list: list, *, permissions=None):
    """Validate/encode a projection; its containers still borrow the supplied tools."""
    if not isinstance(tool_list, list):
        raise ValueError('Native tool catalog requires prepared function definitions')
    if permissions is None:
        from lib.tasks_pkg.tool_dispatch._flags import _task_partitions, _task_confirmation_tools
        writes, _ = _task_partitions(task)
        confirmations = _task_confirmation_tools(task)
    catalog = {}
    declared = set()
    for definition in tool_list:
        if (not isinstance(definition, dict) or definition.get('type') != 'function'
                or set(definition) - {'type','function','decoded_result_schema'}
                or not isinstance(definition.get('function'), dict)):
            raise ValueError('Native tool catalog cannot represent this prepared tool')
        if 'decoded_result_schema' in definition:
            # This is host result metadata, never a provider argument/control.
            # The canonical gateway retains this producer documentation with
            # the prepared definition; the Rust catalog projects inputs only.
            from lib.tools.contracts import serialize_decoded_result_schema
            serialize_decoded_result_schema(definition['decoded_result_schema'])
        function = definition['function']
        name = function.get('name')
        if (not isinstance(name, str) or not name or name in declared
                or set(function) - {'name','description','parameters'}
                or not isinstance(function.get('parameters'), dict)
                or not isinstance(function.get('description',''), str)):
            raise ValueError('Native tool catalog definition is invalid or duplicated')
        if 'decoded_result_schema' in definition:
            from lib.tools.gateway import sanitize_wire_tools
            projected = sanitize_wire_tools([definition])
            if len(projected) != 1:
                raise ValueError('Native tool documentation projection was refused')
            function = dict(function, description=projected[0]['function']['description'])
        declared.add(name)
        if permissions is not None and name not in permissions:
            continue
        permission = permissions[name] if permissions is not None else (
            'approval' if name in confirmations else 'write' if name in writes else 'read')
        if permission not in {'read','write','approval'}:
            raise ValueError('Native tool catalog permission is invalid')
        catalog[name] = {'name':name,'description':function.get('description',''),
            'contractVersion':'tofu.tool-contract/v2','permission':permission,
            'arguments_schema':function['parameters']}
    if permissions is not None and set(permissions) - declared:
        raise ValueError('Native tool permissions are outside the prepared catalog')
    encoded = json.dumps(catalog, allow_nan=False)
    return encoded, catalog


@dataclass(slots=True, eq=False)
class _PendingApproval:
    proof: bytes
    authority: str | None = None
    issued_at: float = 0
    prepared: tuple | None = None


class NativeGateway:
    def __init__(self, *, principal: PrincipalContext, task: dict, executor: Executor,
                 configuration: dict, tool_list: list, permissions: dict | None = None,
                 project_path: str | None, project_enabled: bool, model: str,
                 round_source, decision_policy: ApprovalPolicy | None,
                 pending_calls: int, pending_bytes: int, multimodal: bool = False, search_results=None):
        if type(multimodal) is not bool:
            raise ValueError('native gateway media admission must be explicit')
        self.multimodal=multimodal
        if search_results is not None and not isinstance(search_results,list):
            raise ValueError('native gateway search evidence must be a borrowed list')
        self.search_results=[] if search_results is None else search_results
        self.owner = principal.require_owner(context='native application gateway')
        if (type(task.get('_userId')) is not int or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or not isinstance(executor, Executor) or (round_source is not None and not callable(round_source))
                or type(pending_calls) is not int or pending_calls <= 0
                or type(pending_bytes) is not int or pending_bytes <= 0):
            raise ValueError('native gateway requires explicit owned bounded composition')
        if permissions is None:
            _, catalog = _serialize_native_tool_catalog(task, tool_list)
            permissions = {name: tool['permission'] for name, tool in catalog.items()}
        if any(permission not in {'read', 'write', 'approval'} for permission in permissions.values()):
            raise ValueError('native gateway permission is not declared')
        declared = {item.get('function', item).get('name') for item in tool_list if isinstance(item, dict)}
        if not set(permissions).issubset(declared):
            raise ValueError('native gateway names are not in the prepared application catalog')
        self.task, self.task_id, self.executor = task, task['id'], executor
        # Freeze trusted policy/catalog input without retaining caller containers.
        self.configuration = json.dumps(configuration, allow_nan=False)
        self.tool_list = json.dumps(tool_list, allow_nan=False)
        self.permissions = dict(permissions)
        self.project_path, self.project_enabled, self.model = project_path, project_enabled, model
        self.round_source, self.decision_policy = round_source, decision_policy
        self.pending_calls, self.pending_bytes = pending_calls, pending_bytes
        self._pending, self._bytes = {}, 0
        self._pending_lock = threading.RLock()
        self._lock = asyncio.Lock()
        self._closed = False
        self._response_round = None
        self._response_prepared = {}
        self.response_retained_bytes = 0
        self._progress = None
        self._recovery_effects = None

    def enable_progress(self, limits, feedback_bytes):
        if self._closed or self._progress is not None:
            raise ValueError('native gateway progress already bound or closed')
        from lib.tasks_pkg.native_progress import NativeGatewayProgress
        self._progress = NativeGatewayProgress(self, limits, feedback_bytes)
        return self._progress

    def callbacks(self):
        return {name: self.execute for name in self.permissions}

    def prepare_effects(self, round_num, calls):
        """Bind checkpoint calls to the exact canonical rows already prepared."""
        from lib.tasks_pkg.executor._effects import PreparedToolEffects
        with self._pending_lock:
            if self._closed or self._response_round != round_num or self._pending:
                raise ValueError('native recovery has no settled unentered tool batch')
            entries = {}
            for queue in self._response_prepared.values():
                for parsed, assistant, _ in queue:
                    item = parsed[0]
                    identity = assistant['tool_calls'][0]['id']
                    if identity in entries:
                        raise ValueError('native recovery occurrence is ambiguous')
                    entries[identity] = (item[1], item[2], item[4], item[5], assistant['tool_calls'][0])
            if len(entries) != len(calls) or any(call.get('id') not in entries for call in calls):
                raise ValueError('native recovery requires the complete prepared batch')
            occurrences = []
            for call in calls:
                entry = entries[call['id']]
                if call != entry[4]:
                    raise ValueError('native recovery tool identity changed')
                occurrences.append(entry[:4])
            if self._recovery_effects is not None:
                self._recovery_effects.close()
            batch = PreparedToolEffects(self.task, occurrences)
            batch.attach()
            self._recovery_effects = batch
            return batch

    def native_catalog(self):
        # This decode owns every nested input container and normalizes JSON keys
        # and tuples before projection. Validation still encodes the entire
        # result (including finite-number checks), but a second decode would
        # merely duplicate this already detached, JSON-normalized graph.
        _, catalog = _serialize_native_tool_catalog(
            self.task, json.loads(self.tool_list), permissions=self.permissions)
        return catalog

    def _identity(self, request):
        if (self._closed or request.principal.require_owner(context='native application tool') != self.owner
                or request.task_id != self.task_id or self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int or self.task['_userId'] != self.owner
                or self.task.get('status') not in {'pending', 'running'}
                or request.name not in self.permissions):
            raise ValueError('native application tool ownership changed')

    @staticmethod
    def _proof(name, arguments, caller):
        return json.dumps([name, arguments, caller], ensure_ascii=False,
                          sort_keys=True, allow_nan=False, separators=(',', ':')).encode()

    async def decide(self, request: ApprovalRequest):
        self._identity(request)
        if self.permissions[request.name] == 'read':
            raise ValueError('native approval is not an application write')
        arguments, caller = request.arguments, request.caller
        proof = self._proof(request.name, arguments, caller)
        with self._pending_lock:
            self._identity(request)
            if (request.call_id in self._pending or len(self._pending) >= self.pending_calls
                    or self._bytes + len(proof) > self.pending_bytes):
                raise ValueError('native application approval budget exhausted')
            # Reserve before awaiting policy; workers borrow this exact record.
            reservation = _PendingApproval(proof)
            self._pending[request.call_id] = reservation
            self._bytes += len(proof)
        try:
            if self.decision_policy is None:
                work = asyncio.get_running_loop().run_in_executor(
                    self.executor, copy_context().run, self._decide_application, request, reservation, arguments, caller)
                decision = await self._await_owned(request, work)
            else:
                decision = await self.decision_policy.decide(request)
            self._identity(request)
            if request.cancelled.is_set():
                decision = 'denied'
            if decision == 'denied':
                return decision
            from lib.tasks_pkg.tool_dispatch._approval import _TOOL_APPROVAL_RECEIPT_AUTHORITIES
            if decision not in _TOOL_APPROVAL_RECEIPT_AUTHORITIES:
                raise ValueError('application approval authority is unknown')
            with self._pending_lock:
                if self._pending.get(request.call_id) is not reservation:
                    raise ValueError('native application approval reservation changed')
                reservation.authority, reservation.issued_at = decision, time.time()
            return decision
        finally:
            with self._pending_lock:
                if self._pending.get(request.call_id) is reservation and reservation.authority is None:
                    self._pending.pop(request.call_id)
                    self._bytes -= len(proof)
                    self._release_prepared(reservation)

    async def execute(self, request: ToolRequest):
        self._identity(request)
        async with self._lock:
            self._identity(request)
            approved = None
            if self.permissions[request.name] != 'read':
                proof = self._proof(request.name, request.arguments, request.caller)
                with self._pending_lock:
                    approved = self._pending.pop(request.call_id, None)
                    if approved is not None:
                        self._bytes -= len(approved.proof)
                    if (approved is None or approved.authority is None
                            or approved.proof != proof):
                        if approved is not None:
                            self._release_prepared(approved)
                        raise ValueError('application gateway has no exact unused approval')
            # The native host owns callback cancellation/result joining. This
            # future owns the actual canonical pipeline work through completion.
            work = asyncio.get_running_loop().run_in_executor(
                self.executor, copy_context().run, self._execute, request, approved)
            return await self._await_owned(request, work)

    async def _await_owned(self, request, work):
        stop = asyncio.create_task(request.cancelled.wait()) if request.cancelled is not None else None
        cancelled = False
        try:
            if stop is not None:
                done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    self._stop()
            while True:
                try:
                    result = await asyncio.shield(work)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    self._stop()
                    if work.done():
                        result = work.result()
                        break
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            if stop is not None:
                stop.cancel()
            if not work.done():
                self._stop()
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception as diagnostic_error:
                        logger.debug('_await_owned failure handled: %s', type(diagnostic_error).__name__)
                        break
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)

    def _stop(self):
        # Task/tool contexts already observe these existing owner stop fields.
        self.task['aborted'] = True
        self.task.setdefault('_abort_timestamp', time.time())
        self.task.setdefault('_abort_source', 'native_parent_stop')
        event = self.task.get('abort_event')
        if event is not None:
            event.set()

    def close(self):
        """Dispose ephemeral proofs after the native client has joined callbacks."""
        with self._pending_lock:
            self._closed = True
            self.round_source = None
            for reservation in self._pending.values():
                self._release_prepared(reservation)
            self._pending.clear()
            self._bytes = 0
            self._response_prepared.clear()
            self._response_round = None
            self.response_retained_bytes = 0
            if self._recovery_effects is not None:
                self._recovery_effects.close()
                self._recovery_effects = None
        if self._progress is not None:
            self._progress.close()

    def prepare_response(self, result):
        """Bind one returned declaration before any callback can execute."""
        from collections import defaultdict,deque
        from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls, publish_prepared_response_siblings
        if (result.request.task_id != self.task_id or result.request.principal.require_owner(context='native siblings') != self.owner
                or self.task.get('id') != self.task_id or type(self.task.get('_userId')) is not int
                or self.task['_userId'] != self.owner or self._closed):
            raise ValueError('native sibling declaration ownership changed')
        calls=result.evidence.get('model_calls')
        if not isinstance(calls,list):raise ValueError('native sibling declaration is missing')
        if self.task['abort_event'].is_set():
            return  # Accounting already settled; stopped calls never get tool rows.
        original=json.loads(json.dumps(calls,allow_nan=False))
        from lib.tools.contracts import compile_execution_contract_documents, validate_tool_arguments_from_documents
        documents=None
        assistant={'role':'assistant','content':'','tool_calls':[]}
        proofs=[]
        for call in original:
            if not isinstance(call,dict) or not isinstance(call.get('function'),dict):
                continue
            name=call['function'].get('name')
            if name not in self.permissions:
                continue  # Native admission still owns unknown/invalid calls.
            if documents is None:
                documents=compile_execution_contract_documents(json.loads(self.tool_list),namespace='native')
            try:
                arguments=json.loads(call['function']['arguments'])
                normalized=validate_tool_arguments_from_documents(documents,name,arguments)
            except (KeyError,TypeError,ValueError):
                continue  # Preserve its denied feedback and subsequent model round.
            proofs.append(self._proof(name,normalized,call.get('caller')))
            assistant['tool_calls'].append(call)
        announcements={}
        def retain_entry(task,entry,event,name,number):
            announcements[id(entry)]=(entry,event,name,number)
        parsed,_=parse_tool_calls(assistant,self.task,result.request.round,
            len(self.task.get('toolRounds') or []),self.project_enabled,
            tool_entry_publisher=retain_entry)
        if len(parsed)!=len(proofs):raise ValueError('native sibling parser lost an occurrence')
        prepared=defaultdict(deque)
        for proof,item,call in zip(proofs,parsed,assistant['tool_calls']):
            prepared[proof].append(([item],{'role':'assistant','content':'','tool_calls':[call]},
                announcements.get(id(item[5]))))
        from lib.tasks_pkg.event_delivery import retained_snapshot_bytes
        charge=(sum(len(proof) for proof in proofs)+retained_snapshot_bytes(
            assistant,[call[:5]+call[6:] for call in parsed])) if parsed else 0
        if charge>self.pending_bytes:
            raise ValueError('native sibling declaration exceeds the owned pending-byte bound')
        publish_prepared_response_siblings(self.task,parsed,result.request.round)
        with self._pending_lock:
            if self._pending:raise ValueError('native sibling declaration overlaps pending approval')
            self._response_round=result.request.round
            self._response_prepared=dict(prepared)
            self.response_retained_bytes=charge

    def _release_prepared(self, reservation):
        prepared, reservation.prepared = reservation.prepared, None
        if prepared is not None:
            parsed, _, _, charge = prepared
            self._bytes -= charge
            receipts = self.task.get('_tool_approval_receipts') or {}
            receipts.pop(parsed[0][2], None)

    def _decide_application(self, request, reservation, arguments, caller):
        self._identity(request)
        if self.round_source is None:
            raise ValueError('Application approval requires factual model round identity')
        round_num = self.round_source()
        if type(round_num) is not int or round_num < 0:
            raise ValueError('Application approval model round identity invalid')
        parsed, assistant = self._parse_occurrence(request, round_num, arguments=arguments, caller=caller)
        from lib.tasks_pkg.tool_dispatch.api import authorize_prepared_tool_call, withdraw_prepared_tool_call
        try:
            from lib.tasks_pkg.event_delivery import retained_snapshot_bytes
            # Round entries are already owned by task.toolRounds. Charge the
            # private prepared arguments/carriers, not that borrowed durable row.
            charge = retained_snapshot_bytes(assistant,[call[:5]+call[6:] for call in parsed])
            with self._pending_lock:
                self._identity(request)
                if self._pending.get(request.call_id) is not reservation:
                    raise ValueError('Application approval reservation changed')
                if self._bytes + charge > self.pending_bytes:
                    raise ValueError('Application prepared approval budget exhausted')
                reservation.prepared = (parsed, assistant, round_num, charge)
                self._bytes += charge
            decision = authorize_prepared_tool_call(self.task, parsed[0],
                configuration=json.loads(self.configuration), project_path=self.project_path,
                round_num=round_num, model=self.model)
            # Rebinding may change private arguments; risk metadata stays task-owned.
            current_charge = retained_snapshot_bytes(assistant,[call[:5]+call[6:] for call in parsed])
            with self._pending_lock:
                self._identity(request)
                if self._pending.get(request.call_id) is not reservation:
                    raise ValueError('Application approval reservation changed')
                if self._bytes - charge + current_charge > self.pending_bytes:
                    raise ValueError('Application prepared approval budget exhausted')
                self._bytes += current_charge - charge
                reservation.prepared=(parsed,assistant,round_num,current_charge)
            return decision
        except BaseException as error:
            try:
                withdraw_prepared_tool_call(self.task,parsed[0],status='aborted' if self.task.get('aborted') else 'error')
            except BaseException as cleanup:
                error.add_note(f'Application approval withdrawal failed: {type(cleanup).__name__}')
            raise

    def finish_approvals(self):
        """After native callback join, settle grants not consumed by execution."""
        from lib.tasks_pkg.tool_dispatch.api import withdraw_prepared_tool_call
        with self._pending_lock:
            reservations = tuple(self._pending.values())
            if any(reservation.authority is None for reservation in reservations):
                raise ValueError('Application approval callbacks have not joined')
        for reservation in reservations:
            if reservation.prepared is not None:
                withdraw_prepared_tool_call(self.task,reservation.prepared[0][0])
        with self._pending_lock:
            if tuple(self._pending.values()) != reservations:
                raise ValueError('Application approval ownership changed during withdrawal')
            for reservation in reservations:
                self._release_prepared(reservation)
            self._pending.clear()
            self._bytes = 0

    def _parse_occurrence(self, request, round_num, *, arguments=None, caller=None):
        from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls
        if arguments is None:
            arguments, caller = request.arguments, request.caller
        if self._response_round is not None:
            proof=self._proof(request.name,arguments,caller)
            with self._pending_lock:
                prepared=self._response_prepared.get(proof)
                if round_num!=self._response_round or not prepared:
                    raise ValueError('native callback differs from its settled model declaration')
                parsed,assistant,announcement=prepared.popleft()
                if not prepared:
                    del self._response_prepared[proof]
                if not self._response_prepared:
                    self.response_retained_bytes=0
                if announcement is not None:
                    from lib.tasks_pkg.tool_dispatch.api import publish_tool_call_entry
                    publish_tool_call_entry(self.task,*announcement)
                return parsed,assistant
        identity = f'native-{uuid.uuid4().hex}'
        call = {'id': identity, 'type': 'function', 'function': {
            'name': request.name, 'arguments': json.dumps(dict(arguments), allow_nan=False),
        }}
        if caller is not None:
            call['caller'] = caller
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
        parsed, _ = parse_tool_calls(assistant, self.task, round_num,
                                    len(self.task.get('toolRounds') or []), self.project_enabled)
        if len(parsed) != 1:
            raise RuntimeError('native gateway parser did not retain the occurrence')
        return parsed, assistant

    def _execute(self, request, approved):
        self._identity(request)
        from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
        from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt

        round_num = request.model_round
        if round_num is None and self.round_source is not None:
            round_num = self.round_source()
        if type(round_num) is not int or round_num < 0:
            raise ValueError('native gateway requires factual model round identity')
        prepared = approved.prepared if approved is not None else None
        if prepared is None:
            parsed, assistant = self._parse_occurrence(request, round_num)
        else:
            parsed, assistant, prepared_round, charge = prepared
            with self._pending_lock:
                approved.prepared = None
                self._bytes -= charge
            if prepared_round != round_num:
                raise ValueError('Application approved round identity changed')
        call = assistant['tool_calls'][0]
        parsed_id = parsed[0][2]
        if approved is not None:
            from lib.tasks_pkg.tool_dispatch._approval import approval_receipt_authority
            # The shared decision already minted a live exact receipt. Keep its
            # evidence-bound restrictions when consuming the prepared carrier.
            retained = prepared is not None and approval_receipt_authority(
                self.task, parsed[0][1], parsed_id, parsed[0][3]) == approved.authority
            if not retained:
                if not mint_approval_receipt(self.task, parsed[0][1], parsed_id,
                                             parsed[0][3], authority=approved.authority):
                    raise RuntimeError('application approval receipt could not bind')
                self.task['_tool_approval_receipts'][parsed_id]['minted_at'] = approved.issued_at
        messages = [assistant]
        execute_tool_pipeline(self.task, parsed, json.loads(self.configuration),
            self.project_path, self.project_enabled, json.loads(self.tool_list),
            messages, self.search_results, round_num, self.model, publish_direct_gateway_siblings=False)
        results = [item for item in messages if item.get('role') == 'tool']
        if len(results) != 1 or results[0].get('tool_call_id') != parsed_id:
            raise RuntimeError('canonical gateway did not settle one exact tool result')
        content = results[0].get('content')
        if not isinstance(content, str):
            if not self.multimodal:
                raise ValueError('native feedback requires a declared multimodal adapter')
            from lib.agent_core.native_process import validate_model_content
            validate_model_content(content)
        if self._progress is not None:
            self._progress.record(round_num, call, content)
        return content
