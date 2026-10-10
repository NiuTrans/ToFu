"""Private native-authority harness for the actual knowledge upload facade.

Entry: DurableHtmlCorpus. Dependencies: canonical ingest/store/repository and
embedded TofuDB. Only private function namespaces and owned fixture directories
are substituted; production globals and the configured application remain intact.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import types

from benchmarks.knowledge_html.reference import load_reference
from lib.knowledge import ingest, store
from lib.knowledge.repository import KnowledgeRepository
from lib.identity import require_user_id
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from serverctl_pkg.native_engine import source_fingerprint
from tofu_db.embedded import EmbeddedRuntime


class DurableHtmlCorpus:
    def __init__(self, root: Path, engine: Path, *, variant: str, owner_user_id: int = 1, initialize: bool = True):
        if variant not in {'reference', 'candidate'}:
            raise ValueError('unknown HTML variant')
        self.root = root
        self.owner_user_id = require_user_id(owner_user_id, context='private native corpus owner')
        self.engine = engine
        self.runtime = None
        self.client = None
        self.data_dir = root / 'authority'
        if not root.is_dir():
            raise ValueError('caller must supply an owned existing fixture directory')
        if type(initialize) is not bool:
            raise TypeError('authority initialization must be explicit boolean')
        if initialize:
            subprocess.run([str(engine), 'init', '--data-dir', str(self.data_dir)],
                           check=True, capture_output=True, timeout=60)
        elif not self.data_dir.is_dir():
            raise ValueError('cold recovery requires an existing caller-owned authority')
        self.open()
        try:
            reference, self.reference_source_sha256 = load_reference()
            ingest_namespace = dict(vars(ingest))
            if variant == 'reference':
                ingest_namespace['_html_to_text'] = reference
            extract = types.FunctionType(ingest.extract.__code__, ingest_namespace,
                                         ingest.extract.__name__, ingest.extract.__defaults__)
            extract.__kwdefaults__ = ingest.extract.__kwdefaults__
            namespace = dict(vars(store))
            for name, value in vars(store).items():
                if isinstance(value, types.FunctionType) and value.__globals__ is vars(store):
                    clone = types.FunctionType(value.__code__, namespace, value.__name__,
                                               value.__defaults__, value.__closure__)
                    clone.__kwdefaults__ = value.__kwdefaults__
                    namespace[name] = clone
            namespace.update(extract=extract, _repository=self.repository,
                             _SOURCE_ROOT_OVERRIDE=str(root / 'sources'),
                             _ASSET_ROOT_OVERRIDE=str(root / 'assets'))
            self.add_document = namespace['add_document']
            self.get_document_content = namespace['get_document_content']
        except BaseException:
            self.close()
            raise

    def open(self):
        if self.runtime is not None:
            raise RuntimeError('authority is already open')
        secret = b'html-benchmark-private-authority-secret-20261008'
        runtime = EmbeddedRuntime(self.engine.with_name('libtofu_db.so'), self.data_dir,
                                  owner_id=self.owner_user_id, tenant_id=1, secret=secret,
                                  projection_dir=self.root / 'projection')
        try:
            checkout = Path(__file__).resolve().parents[2]
            identity = runtime.build_identity()
            expected = source_fingerprint(checkout)
            if expected is None or identity != {'source_sha256': expected}:
                raise RuntimeError('native library is not built from this checkout')
            self.build_identity = identity
            self.library_sha256 = hashlib.sha256(
                self.engine.with_name('libtofu_db.so').read_bytes()).hexdigest()
            self.client = EmbeddedStorageV2Client(runtime, secret, owner_id=self.owner_user_id,
                                                  tenant_id=1, timeout=30)
            self.runtime = runtime
            health = self.client.health(deadline=30)
            if not health['ready'] or health['backend'] != 'tofudb':
                raise RuntimeError('private native authority is not ready')
            self.resource_budget = health['resourceBudget']
        except BaseException:
            runtime.close()
            self.runtime = None
            raise

    def repository(self, user_id: int):
        return KnowledgeRepository(user_id, client_factory=lambda **_: self.client)

    def close(self):
        try:
            if self.client is not None:
                self.client.close()
                self.client = None
        finally:
            if self.runtime is not None:
                runtime = self.runtime
                runtime.close()
                if not runtime.closed:
                    raise RuntimeError('private authority did not close')
                self.runtime = None


def semantic_projection(page: dict) -> dict:
    """Compare content and public metadata while excluding generated IDs/clocks."""
    assert not page['pagination']['has_more']
    return {**page, 'document': {key: value for key, value in page['document'].items()
                                if key not in {'id', 'created_at', 'updated_at'}}}
