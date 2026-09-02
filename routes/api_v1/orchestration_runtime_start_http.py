"""Shared HTTP application adapter for every orchestration start mode."""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.orchestration.application_provider_ports import (
    DefinitionResolver,
    RuntimeStartServiceProvider,
)

from .orchestration_run_http import (
    prepare_run_request,
    run_start_response,
)
from .orchestration_request_http import orchestration_request_response
from .orchestration_service_http import orchestration_service_response
from .auth import request_user_id


logger = get_logger(__name__)


def runtime_start_request_response(
    context: str,
    kind: str,
    body: dict,
    *,
    resolve_definition: DefinitionResolver,
    runtime_start_service: RuntimeStartServiceProvider,
    created_by: str = '',
) -> Any:
    """Resolve, start and project one ephemeral or durable HTTP request."""
    def handle(prepared):
        definition = prepared.definition

        def project_result(runtime_id: str):
            assert runtime_id
            logger.info(
                '[Orchestrations] %s START id=%s name=%r source=%s',
                kind,
                runtime_id,
                definition.get('name'),
                prepared.definition_source,
            )
            return run_start_response(prepared, runtime_id, kind=kind)

        return orchestration_service_response(
            context,
            lambda: runtime_start_service().start(
                kind,
                definition,
                owner_user_id=int(request_user_id()),
                input_text=prepared.input_text,
                orchestration_id=prepared.orchestration_id,
                created_by=created_by,
            ),
            project_result,
        )

    return orchestration_request_response(
        prepare_run_request(
            body,
            resolve_definition=resolve_definition,
        ),
        handle,
    )


__all__ = ['runtime_start_request_response']
