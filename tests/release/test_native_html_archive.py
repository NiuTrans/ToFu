"""HTML release capability is independently declared, pinned and lazily borrowed."""
import asyncio
from dataclasses import replace
import hashlib
import itertools
import json
import os
from pathlib import Path
import pytest

from scripts.build_native_runtime import SPEC, write_archive
from lib.agent_core.native_artifact import inspect_archive, seal_html_projection_archive
from lib.agent_core.native_html_projection_contract import SPEC as HTML_SPEC
from lib.agent_core.native_token_count_contract import TOKEN_COUNT_SPEC
from lib.orchestration.native_flow_contract import SPEC as FLOW_SPEC
from lib.agent_core.native_process import NativeConfigurationError
from tests.release.test_native_runtime_archive import members

pytestmark = pytest.mark.unit


def archive(tmp_path, html, flow, token, mutation=None):
    files = members()
    for enabled, key, contract in ((html, 'optional_html_projection', HTML_SPEC),
                                  (flow, 'optional_flow', FLOW_SPEC),
                                  (token, 'optional_token_count', TOKEN_COUNT_SPEC)):
        if enabled:
            extension = SPEC[key]
            files[extension['member']] = b'\x7fELFlibrary'
            files[extension['contract_member']] = json.dumps(contract).encode()
    if mutation: mutation(files)
    manifest = json.loads(files['manifest.json'])
    manifest['files'] = {name:dict(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
                        for name,raw in files.items() if name != 'manifest.json'}
    files['manifest.json'] = json.dumps(manifest).encode()
    path = tmp_path / 'native.tar.gz'
    write_archive(files, path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('html,flow,token', list(itertools.product([False, True], repeat=3)))
def test_html_selection_requires_its_own_declared_contract(tmp_path, html, flow, token):
    path, pin = archive(tmp_path, html, flow, token)
    inspect_archive(path)
    if html:
        with seal_html_projection_archive(path, pin, 'fixture', 1024) as image:
            assert image.read() == b'\x7fELFlibrary'
            with pytest.raises(PermissionError): os.write(image.fileno(), b'x')
    else:
        with pytest.raises(NativeConfigurationError, match='selected .* missing'):
            seal_html_projection_archive(path, pin, 'fixture', 1024)


@pytest.mark.parametrize('mutation', [
    lambda f: f.pop(SPEC['optional_html_projection']['member']),
    lambda f: f.pop(SPEC['optional_html_projection']['contract_member']),
    lambda f: f.__setitem__(SPEC['optional_html_projection']['contract_member'],
                           json.dumps(dict(HTML_SPEC, abi_version=99)).encode()),
    lambda f: f.__setitem__(SPEC['optional_html_projection']['member'], b'not ELF'),
])
def test_malformed_html_release_refuses_before_loading(tmp_path, mutation):
    path, pin = archive(tmp_path, True, False, False, mutation)
    with pytest.raises(NativeConfigurationError):
        seal_html_projection_archive(path, pin, 'fixture', 1024)


def test_html_installation_is_pinned_exclusive_and_does_not_require_other_abis(tmp_path):
    from scripts.build_native_runtime import install_html_library
    path, pin = archive(tmp_path, True, False, False)
    destination = tmp_path/'installed.so'
    with pytest.raises(NativeConfigurationError, match='checksum'):
        install_html_library(path, destination, archive_sha256='0'*64, application_version='fixture')
    assert not destination.exists()
    receipt = install_html_library(path, destination, archive_sha256=pin, application_version='fixture')
    assert destination.read_bytes() == b'\x7fELFlibrary'
    assert receipt['html_library_sha256'] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert receipt['bytes'] == len(destination.read_bytes())
    with pytest.raises(FileExistsError):
        install_html_library(path, destination, archive_sha256=pin, application_version='fixture')
    assert destination.read_bytes() == b'\x7fELFlibrary'


def test_installed_combined_archive_borrows_one_sealed_library_and_preserves_lifetimes():
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_token_count import NativeTokenCounter
    from lib.orchestration.native_flow import NativeFlowLibrary
    from lib.knowledge.native_html import NativeHtmlProjector
    from tests.agent_core.test_native_process import budget
    value = os.environ.get('TOFU_NATIVE_HTML_TEST_ARCHIVE')
    if not value: pytest.skip('explicit combined HTML/Flow/token release required')
    path = Path(value)
    manifest, _ = inspect_archive(path)
    before = set(os.listdir('/proc/self/fd'))
    image = NativeArchiveImage(archive=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              application_version=manifest['build']['application_version'],
                              budget=replace(budget(), binary_bytes=64<<20))
    html = token = flow = None
    try:
        html = NativeHtmlProjector.from_shared_archive(image=image)
        token = NativeTokenCounter.from_shared_archive(image=image)
        flow = NativeFlowLibrary.from_shared_archive(image=image)
        assert len(image._libraries) == 1
        assert os.fstat(html._image.fileno()).st_ino == os.fstat(token._image.fileno()).st_ino
        asyncio.run(image.close())
        assert html.project('<h1>owned</h1>') == '# owned'
        assert token.count_batch(['hello world'], encoding='cl100k_base') == [2]
        token.close()
        assert html.project('<p>independent</p>') == 'independent'
    finally:
        for binding in (html, token, flow):
            if binding is not None: binding.close()
        # close was already joined in the successful path; no second event-loop bind.
        if not image._closed: asyncio.run(image.close())
    assert set(os.listdir('/proc/self/fd')) == before
