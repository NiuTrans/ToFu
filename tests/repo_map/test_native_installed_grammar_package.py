"""Installed-only grammar integrity, lazy admission, and frozen package identity."""
import os
import pytest
from tests.repo_map.test_native_manifest import private_package as private_package, run_fresh

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def require_bundle():
    package = pytest.importorskip('tofu_repo_kernel')
    with package._load_lock:
        _, record = package._package_manifest()
        available = 'grammar_bundle' in record['stream']
    if os.environ.get('TOFU_REQUIRE_INSTALLED_GRAMMARS') == '1':assert available
    elif not available:pytest.skip('an explicit installed pinned grammar bundle is required')


def test_opening_image_keeps_grammar_code_lazy(private_package):
    run_fresh(private_package, '''
from pathlib import Path
import tofu_repo_kernel as package
import tofu_repo_kernel._stream as stream
original=Path.open
def opened(path,*args,**kwargs):
    assert path.parent.name!='grammars', 'image admission read grammar code or manifest'
    return original(path,*args,**kwargs)
Path.open=opened
stream.subprocess.Popen=lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('image admission started a child'))
with package.open_stream_process() as executable:
    assert executable._installed_grammars is not None
assert executable._image.closed
''')


def test_actual_installed_probe_checks_all_grammars(private_package):
    run_fresh(private_package, '''
import json
from pathlib import Path
import tofu_repo_kernel as package
from tofu_repo_kernel.__main__ import check_grammars
root=Path(package.__file__).parent
manifest=json.loads((root/'grammars/grammars.json').read_text())
result=check_grammars()
assert result==dict(libraries=len(manifest['libraries']),bytes=sum(item['bytes'] for item in manifest['libraries']))
assert result['libraries']>0
''')


def test_frozen_package_cannot_repin_changed_grammar_manifest(private_package):
    run_fresh(private_package, '''
import hashlib,json
from pathlib import Path
import tofu_repo_kernel as package
from tofu_repo_kernel._process_discovery import retire_process
root=Path(package.__file__).parent
with package.open_stream_process() as original:
    content=b'{}';(root/'grammars/grammars.json').write_bytes(content)
    record=json.loads((root/'build.json').read_text())
    record['stream']['grammar_bundle'].update(sha256=hashlib.sha256(content).hexdigest(),bytes=len(content))
    (root/'build.json').write_text(json.dumps(record))
    with package.open_stream_process() as subsequent:
        for image in [original,subsequent]:
            process=image.start_grammar_check()
            try:
                output,errors=process.communicate(timeout=10)
                assert process.returncode!=0 and not output
            finally:retire_process(process)
assert original._image.closed and subsequent._image.closed
''')


def test_contract_replacement_refuses_before_child_launch(private_package):
    run_fresh(private_package, '''
from pathlib import Path
import tofu_repo_kernel as package
root=Path(package.__file__).parent;path=root/'grammar_bundle.schema.json'
path.write_text(path.read_text()+' ')
try:package.open_stream_process()
except ValueError as error:assert 'contract digest differs' in str(error)
else:raise AssertionError('changed grammar contract admitted')
''')


def test_absent_bundle_refuses_explicit_installed_selection(private_package):
    run_fresh(private_package, '''
import json
from pathlib import Path
import tofu_repo_kernel as package
path=Path(package.__file__).parent/'build.json';record=json.loads(path.read_text())
record['stream'].pop('grammar_bundle');path.write_text(json.dumps(record))
with package.open_stream_process() as image:
    try:image.start('.',None,1_000_000_000,(1<<20,)*4)
    except ValueError as error:assert 'no installed grammar bundle' in str(error)
    else:raise AssertionError('missing installed bundle silently fell back')
''')
