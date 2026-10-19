"""Owned native execution/visibility binding over the shared tool policy.

prepared_execution_tools freezes task authority plus declared local gateway
contracts. NativeToolProjection borrows RootLoopRequest/executor and prepares
the final model-visible surface off the serving loop, joining cancellation.
No second tool handler, registry, pool, storage or model loop is created.
"""
import asyncio
from contextvars import copy_context
import json

from lib.agent_core.native_process import _joined

from lib.log import get_logger

logger = get_logger(__name__)


def prepared_execution_tools(task, visible):
    source = task.get('_executable_tool_catalog')
    authority = visible if not isinstance(source, list) else source
    names = {item['function']['name'] for item in authority}
    tools = list(authority)
    if tools:
        from lib.context_experiment_flags import normalize_context_experiment_flags
        from lib.tasks_pkg.orchestrator._tool_assembly_prep import attach_gateway_contract_documents
        from lib.tools.gateway import gateway_tool_schemas
        if '_tool_gateway_names' not in task:
            mode = normalize_context_experiment_flags(task.get('config') or {})['tools']['toolSearch']
            if not attach_gateway_contract_documents(task, mode):
                raise ValueError('native gateway contracts could not be prepared')
        declared = set(task['_tool_gateway_names'])
        for tool in gateway_tool_schemas(include_search=True, include_execute=True, include_program_action=True):
            name = tool['function']['name']
            if name in declared and name not in names:
                tools.append(tool)
                names.add(name)
    # Visibility may already contain the shared discovery gateway. Accept only
    # canonical gateways declared by ordinary assembly, never arbitrary names
    # from the visible projection or an empty execution authority.
    if any(item['function']['name'] not in names for item in visible):
        raise ValueError('native visible tools are outside task execution authority')
    if tools:
        # Attaching gateways selects strict document-based ingestion. Explicit
        # prepared callers may not have run full ROOT assembly; their declared
        # execution surface must cross the same canonical contract adapter.
        documents = task.setdefault('_toolContractDocumentsByName', {})
        if not isinstance(documents, dict):
            raise ValueError('native prepared execution contracts must be a mapping')
        missing = [tool for tool in tools if tool['function']['name'] not in documents]
        if missing:
            from lib.tools.contracts import compile_execution_contract_documents
            documents.update(compile_execution_contract_documents(missing, namespace='native'))
    return json.loads(json.dumps(tools, allow_nan=False))


class NativeToolProjection:
    def __init__(self, request, executor):
        self.request, self.executor = request, executor
        self.task_id, self.owner = request.task['id'], request.task['_userId']

    def _check(self):
        task = self.request.task
        if (task.get('id') != self.task_id or type(task.get('_userId')) is not int
                or task['_userId'] != self.owner or task.get('status') not in {'pending','running'}):
            raise ValueError('native wire preparation ownership changed')
        if task['abort_event'].is_set():
            raise asyncio.CancelledError

    def _prepare(self, metadata, template, catalog, messages=None, round_num=0):
        self._check()
        task = self.request.task
        if metadata['owner_user_id'] != self.owner:
            raise ValueError('native wire route owner differs')
        from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
        from lib.llm.tool_wire import project_tool_wire
        body = dict(template, tools=self.request.tool_list or [])
        attach_tool_request_metadata(task, body, self.request.messages if messages is None else messages, self.request.tool_list or [],
            model=metadata['logical_model'], round_num=round_num, tid=self.task_id[:8])
        project_tool_wire(body, api_protocol=metadata['protocol'], base_url=metadata['base_url'],
            oauth='codex' if metadata.get('credential_kind')=='oauth' else '',
            responses_profile=metadata.get('responses_profile') or 'compatible',
            inference_profile=metadata['inference_profile'])
        self._check()
        tools = body.get('tools') or []
        if any(tool.get('type')!='function' or (tool.get('function') or {}).get('name') not in catalog for tool in tools):
            raise ValueError('native wire projection requires declared function tools')
        return tools

    def project(self, metadata, template, catalog, messages, round_num):
        """Called by the owned context worker after injection and admission."""
        return self._prepare(metadata,template,catalog,messages,round_num)

    def __call__(self, metadata, template, catalog, messages, round_num):
        return self.project(metadata, template, catalog, messages, round_num)

    @staticmethod
    def catalog_for_projection(catalog):
        """This built-in visibility observer consumes declared names only.

        Preserve constant-time membership; full argument/permission contracts
        remain with the gateway and native execution configuration.
        """
        return dict.fromkeys(catalog)

    async def prepare(self, metadata, template, catalog):
        self._check()
        worker = self.executor.submit(copy_context().run, self._prepare, metadata, template, catalog)
        future = asyncio.wrap_future(worker)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.request.task['abort_event'].set()
            worker.cancel()
            try:
                await _joined(future)
            except BaseException as diagnostic_error:
                logger.debug('prepare failure handled: %s', type(diagnostic_error).__name__)
                pass
            raise
