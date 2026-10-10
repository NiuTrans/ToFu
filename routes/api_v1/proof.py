"""routes/api_v1/proof.py — Proof-carrying deliverable export + verification.

GET  /api/v1/tasks/{id}/proof — assemble a content-addressed proof bundle
                                for one owned task. 404 keeps foreign and
                                missing tasks indistinguishable.
POST /api/v1/proof/verify     — verify a submitted bundle. Structural checks
                                always; when the caller owns the bundle the
                                server additionally re-reads the live evidence
                                authorities (``verificationScope='full'``).

Wire authority: ``contracts/deliverable_proof_v1.schema.json``. Design
record: ``docs/PROOF_DELIVERABLES_DESIGN_PROPOSAL.md``.
"""

from __future__ import annotations

import orjson
from quart import Blueprint

from lib.api_response import (
    api_internal_error, api_not_found, api_ok, api_payload_too_large,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import async_parse_body, require_dict

from .auth import request_user_id, require_scope

logger = get_logger(__name__)

api_v1_proof_bp = Blueprint('api_v1_proof', __name__)


@api_v1_proof_bp.route('/api/v1/tasks/<task_id>/proof', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Proof bundle: export verifiable deliverable evidence',
          tags=['tasks', 'proof'], scope='tasks')
def task_proof_export(task_id):
    """Assemble the proof bundle for one owned task (read-only, bounded)."""
    from lib.proof import assemble_task_proof
    owner_user_id = int(request_user_id())
    try:
        bundle = assemble_task_proof(task_id, owner_user_id=owner_user_id)
    except Exception as exc:
        logger.error('[api_v1.proof] export failed task=%s: %s',
                     task_id[:8], exc, exc_info=True)
        return api_internal_error('internal_error')
    if bundle is None:
        return api_not_found('Task not found')
    return api_ok({'proof': bundle})


@api_v1_proof_bp.route('/api/v1/proof/verify', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Proof bundle: verify a submitted bundle',
          tags=['proof'], scope='tasks')
async def proof_verify():
    """Verify bundle structure always; re-check evidence for its owner."""
    body = await async_parse_body(strict=True, exact=('bundle',))
    bundle = require_dict(body, 'bundle')
    from lib.proof import verify_proof_bundle
    from lib.proof.contract import limit
    from lib.proof.evidence import server_evidence_resolver
    if len(orjson.dumps(bundle)) > limit('maxBundleBytes'):
        return api_payload_too_large(limit('maxBundleBytes'))
    owner_user_id = int(request_user_id())
    bundle_owner = bundle.get('owner')
    owner_matches = isinstance(bundle_owner, int) \
        and not isinstance(bundle_owner, bool) \
        and bundle_owner == owner_user_id
    resolver = server_evidence_resolver(owner_user_id) if owner_matches else None
    try:
        result = verify_proof_bundle(bundle, resolver=resolver)
    except Exception as exc:
        logger.error('[api_v1.proof] verify failed: %s', exc, exc_info=True)
        return api_internal_error('internal_error')
    return api_ok({'verification': result})
