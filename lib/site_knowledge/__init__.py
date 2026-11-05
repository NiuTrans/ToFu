"""Owned rule facade and read-only access to unassigned legacy candidates.

Runtime readers supply a SiteKnowledgeRepository; publication requires the
revision observed before live verification. Legacy JSON is never executed or
assigned implicitly. adopt_legacy explicitly verifies one candidate for the
caller's already-admitted browser before publishing it; it preserves the file.
"""
from __future__ import annotations

from copy import deepcopy
import os
import time

from lib.json_store import read_json
from lib.site_knowledge.repository import SiteKnowledgeRepository

_STORE_PATH = os.path.join('data', 'config', 'site_knowledge.json')


def get_knowledge(domain: str, operation: str = 'search', *,
                  repository: SiteKnowledgeRepository | None = None) -> dict | None:
    if repository is None:
        return None
    revision = repository.read(domain, operation)
    entry = revision.entry
    return {**entry, 'version': revision.version} if entry is not None else None


def pin_knowledge(domain: str, *, repository: SiteKnowledgeRepository,
                  expected_version: int, extractor_js: str, wait_selector: str = '',
                  scrolls: int = 2, verified_by: str = 'site-doctor',
                  evidence: dict | None = None, notes: str = '',
                  operation: str = 'search', access: str = 'read') -> dict:
    entry = {'extractor_js': extractor_js, 'wait_selector': wait_selector,
             'scrolls': scrolls, 'verified_at': time.time(), 'verified_by': verified_by,
             'evidence': deepcopy(evidence or {}), 'notes': notes, 'access': access}
    revision = repository.publish(domain, entry, expected_version=expected_version,
                                   operation=operation)
    return {**revision.entry, 'version': revision.version}


def clear_knowledge(domain: str, *, repository: SiteKnowledgeRepository,
                    expected_version: int, operation: str = 'search') -> None:
    repository.retire(domain, expected_version=expected_version, operation=operation)


def legacy_candidates() -> dict:
    """Unassigned candidates for operator migration; never a runtime fallback."""
    value = read_json(_STORE_PATH, default={})
    return deepcopy(value) if isinstance(value, dict) else {}


def adopt_legacy(domain: str, *, repository: SiteKnowledgeRepository,
                 scrape, verified_by: str, operation: str = 'search') -> dict:
    """Explicitly adopt one legacy candidate after a fresh, owned page proof.

    scrape is an admitted callable taking the candidate's selector/JS/scrolls;
    its caller supplies the exact target URL and browser authority. A revision
    change while checking refuses publication; no old rule or version is erased.
    """
    from lib.site_doctor.verification import shape_errors
    revision = repository.read(domain, operation)
    site = legacy_candidates().get(domain) or {}
    entry = (site.get('operations', {}).get(operation)
             if isinstance(site.get('operations'), dict) else
             site if operation == 'search' else None)
    if not isinstance(entry, dict) or not str(entry.get('extractor_js') or '').strip():
        raise ValueError('No legacy candidate for this operation')
    candidate = {key: deepcopy(entry.get(key, default)) for key, default in (
        ('wait_selector', ''), ('extractor_js', ''), ('scrolls', 0))}
    # Validate before any executable input reaches a browser.
    from jsonschema import Draft202012Validator
    from lib.site_knowledge.repository import knowledge_contract
    schema = knowledge_contract()['$defs']['entry']
    Draft202012Validator(schema).validate({**candidate, 'access': 'read',
        'verified_at': 0, 'verified_by': verified_by, 'evidence': {}, 'notes': ''})
    result = scrape(**candidate)
    error = shape_errors(result)
    if error:
        raise ValueError('Legacy candidate verification failed: ' + error)
    return pin_knowledge(domain, repository=repository, expected_version=revision.version,
        operation=operation, **candidate, verified_by=verified_by,
        evidence={'verified_count': len(result), 'migration': 'unassigned-legacy'},
        notes=str(entry.get('notes') or ''))
