"""Installed stream image admission, independent owners and standalone probing."""
import os
import pytest
from tests.repo_map.test_native_manifest import private_package as private_package, run_fresh

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def require_packaged_stream():
    pytest.importorskip('tofu_repo_kernel._stream')


def test_import_is_inert_and_opened_images_are_independent(private_package):
    run_fresh(private_package, '''
import sys
import tofu_repo_kernel as package
assert package._manifest is None
assert 'tofu_repo_kernel._stream' not in sys.modules
assert not {'lib', 'numpy', 'scipy', 'tree_sitter_language_pack'} & sys.modules.keys()
a = package.open_stream_process()
b = package.open_stream_process()
try:
    assert a is not b and a._image.fileno() != b._image.fileno()
    a.close()
    child = b.start('.', '.', 0, (1, 1, 1, 1))
    try:
        output, errors = child.communicate(timeout=10)
        assert child.returncode == 3 and not output and not errors
    finally:
        from tofu_repo_kernel._process_discovery import retire_process
        retire_process(child)
finally:
    a.close(); b.close()
assert a._image.closed and b._image.closed
''')


@pytest.mark.parametrize('field,value', [('protocol_version', 1), ('protocol_version', 2), ('protocol_version', True),
                                         ('protocol_version', '1'), ('executable', '../worker')])
def test_stream_manifest_refuses_foreign_protocol_or_path(private_package, field, value):
    run_fresh(private_package, f'''
from pathlib import Path
import json
import tofu_repo_kernel as package
path = Path(package.__file__).parent/'build.json'
record = json.loads(path.read_text());record['stream'][{field!r}] = {value!r}
path.write_text(json.dumps(record))
try:package.open_stream_process()
except ValueError as error:assert 'stream manifest' in str(error)
else:raise AssertionError('invalid stream manifest admitted')
''')


def test_stream_snapshot_and_seal_refuse_replaced_installed_bytes(private_package):
    run_fresh(private_package, '''
from pathlib import Path
import hashlib,json
import tofu_repo_kernel as package
from tofu_repo_kernel._process_discovery import retire_process
root=Path(package.__file__).parent
with package.open_stream_process() as admitted:
    record=json.loads((root/'build.json').read_text())
    path=root/record['stream']['executable'];path.write_bytes(b'replaced')
    record['stream']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    (root/'build.json').write_text(json.dumps(record))
    try:package.open_stream_process()
    except ValueError as error:assert 'digest differs' in str(error)
    else:raise AssertionError('replacement admitted by frozen manifest')
    child=admitted.start('.', '.', 0, (1,1,1,1))
    try:
        output,errors=child.communicate(timeout=10)
        assert child.returncode==3 and not output and not errors
    finally:retire_process(child)
assert admitted._image.closed
''')


@pytest.mark.parametrize('timeout,limits', [(-1, (1,1,1,1)), (True, (1,1,1,1)),
    (1 << 64, (1,1,1,1)), (1, (1,1,1)), (1, (1,1,1,0)), (1, (1,1,1,True))])
def test_invalid_stream_arguments_do_not_launch(private_package, timeout, limits):
    run_fresh(private_package, f'''
import tofu_repo_kernel as package
import tofu_repo_kernel._stream as stream
with package.open_stream_process() as executable:
    def refuse(*args,**kwargs):raise AssertionError('launched invalid request')
    stream.subprocess.Popen=refuse
    try:executable.start('.', '.', {timeout!r}, {limits!r})
    except ValueError:pass
    else:raise AssertionError('invalid arguments admitted')
''')


def test_public_stream_context_retries_close_and_preserves_primary(private_package):
    run_fresh(private_package, '''
import tofu_repo_kernel as package
owner=package.open_stream_process();close=owner.close;attempts=[]
def interrupted():
    attempts.append(True)
    if len(attempts)<=2:raise KeyboardInterrupt('close interrupted')
    close()
owner.close=interrupted
primary=LookupError('original failure')
try:
    with owner:raise primary
except LookupError as error:
    assert error is primary and any('KeyboardInterrupt' in note for note in error.__notes__)
else:raise AssertionError('lost original failure')
assert len(attempts)==3 and owner._image.closed
''')


def test_installed_stream_certification_uses_no_external_git_or_grammars(private_package):
    run_fresh(private_package, '''
import os
from tofu_repo_kernel.__main__ import check_stream
os.environ['PATH']=''
for name in tuple(os.environ):
    if name.startswith('GIT_'):del os.environ[name]
os.environ['GIT_CONFIG_NOSYSTEM']='1'
os.environ['GIT_CONFIG_GLOBAL']='/nonexistent/foreign-config'
os.environ['GIT_DIR']='/nonexistent/foreign-repo'
check_stream()
''')


def test_certification_does_not_claim_stream_probe_on_unsupported_host(private_package):
    run_fresh(private_package, """
import contextlib,io
import tofu_repo_kernel.__main__ as probe
probe.sys.platform='darwin'
def refuse():raise AssertionError('unsupported stream was probed')
probe.check_stream=refuse
output=io.StringIO()
with contextlib.redirect_stdout(output):probe.main()
assert 'sealed stream process unsupported on darwin' in output.getvalue()
assert 'discovery and stream artifacts ready' not in output.getvalue()
""")
