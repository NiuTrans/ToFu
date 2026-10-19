"""Canonical rg/walk membership, empty authority and directly joined producers."""
import os
from pathlib import Path
import shutil
import shlex
import threading
import time

import pytest
from test_native_extraction_stream import stream as stream, observe_children, assert_joined

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('discovery', ['rg', 'walk', 'rg_empty', 'git_empty'])
def test_plain_membership_matches_reference(stream, monkeypatch, tmp_path, discovery):
    from lib.repo_map import outline
    scanner, git_root = stream
    root = git_root if discovery == 'git_empty' else tmp_path/'plain'
    root.mkdir(exist_ok=True)
    names = ['visible.py', 'git_ignored.py', 'ignore_ignored.py', 'rg_ignored.py',
             '.hidden.py', 'node_modules/vendor.py', 'deep/a/b/c/d/e/f/g/h/i/nine.py']
    for name in names:
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def visible_symbol(): pass\n')
    (root/'.gitignore').write_text('*\n' if discovery == 'git_empty' else 'git_ignored.py\n')
    (root/'.ignore').write_text('*\n' if discovery == 'rg_empty' else 'ignore_ignored.py\n')
    (root/'.rgignore').write_text('rg_ignored.py\n')
    (root/'link.py').symlink_to(root/'visible.py')
    (root/'linkdir').symlink_to(root/'node_modules', target_is_directory=True)
    monkeypatch.delenv('RIPGREP_CONFIG_PATH', raising=False)
    if discovery == 'walk':
        monkeypatch.setenv('PATH', str(tmp_path/'absent-binaries'))
    elif not shutil.which('rg'):
        pytest.skip('rg is required for canonical ignore semantics')
    reference = outline.scan(str(root), include_tests=True)
    children = observe_children(monkeypatch)
    actual = scanner(root, include_tests=True)
    assert actual.files == reference.files
    assert actual.tags == reference.tags
    assert actual.references == reference.references
    assert len(children) == (1 if discovery == 'git_empty' else 2)
    assert_joined(children)
    if discovery.endswith('empty'):
        assert not actual.files and not actual.tags


@pytest.mark.parametrize('cause', ['cancel', 'deadline'])
def test_blocked_rg_is_a_directly_joined_child(stream, monkeypatch, tmp_path, cause):
    from tofu_repo_kernel._process_discovery import DiscoveryCancelled, DiscoveryTimeout
    scanner, _ = stream
    root = tmp_path/'blocked-rg';root.mkdir()
    commands = tmp_path/'commands';commands.mkdir()
    marker = tmp_path/'rg.pid'
    fifo = tmp_path/'blocked-input';os.mkfifo(fifo)
    executable = commands/'rg'
    executable.write_text("#!/bin/sh\nprintf '%s' \"$$\" > " + shlex.quote(str(marker))
                          + '\nread value < ' + shlex.quote(str(fifo)) + '\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(commands))
    cancellation = threading.Event()
    children = observe_children(monkeypatch)
    joined = threading.Event()

    def cancel_after_rg_enters():
        deadline = time.monotonic()+5
        while not marker.exists() and not joined.wait(.01):
            if time.monotonic() >= deadline:
                return
        if cause == 'cancel' and marker.exists():
            cancellation.set()

    observer = threading.Thread(target=cancel_after_rg_enters)
    observer.start()
    try:
        with pytest.raises(DiscoveryCancelled if cause == 'cancel' else DiscoveryTimeout):
            scanner(root, timeout=5 if cause == 'cancel' else 1, cancellation=cancellation)
    finally:
        joined.set();observer.join(2)
    assert not observer.is_alive()
    assert len(children) == 2 and int(marker.read_text()) == children[1].pid
    assert_joined(children)


def test_rg_configuration_can_select_hidden_files(stream, monkeypatch, tmp_path):
    from lib.repo_map import outline
    if not shutil.which('rg'):
        pytest.skip('rg required')
    scanner, _ = stream
    root = tmp_path/'plain-configured';root.mkdir()
    (root/'.hidden.py').write_text('def hidden_symbol(): pass\n')
    config = tmp_path/'rg-config';config.write_text('--hidden\n')
    monkeypatch.setenv('RIPGREP_CONFIG_PATH', str(config))
    reference = outline.scan(str(root))
    assert reference.files == ['.hidden.py']
    children = observe_children(monkeypatch)
    actual = scanner(root)
    assert actual.files == reference.files and actual.tags == reference.tags
    assert_joined(children)


@pytest.mark.parametrize('discovery', ['walk', 'git', 'rg'])
@pytest.mark.parametrize('selection', ['all', 'focus', 'subdirectory', 'no_refs'])
def test_byte_paths_keep_reference_identity_and_never_conflate_walk_files(stream, monkeypatch, tmp_path, discovery, selection):
    import subprocess
    from lib.repo_map import outline
    scanner, _ = stream
    root = Path(os.fsdecode(os.fsencode(tmp_path)+b'/byte-root-\xff'))
    root.mkdir()
    if discovery == 'git':
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
    elif discovery == 'rg' and not shutil.which('rg'):
        pytest.skip('rg required')
    files = {
        b'bad-\xff.py': b'def original_python(): pass\n',
        b'bad-\xef\xbf\xbd.py': b'def replacement_python(): pass\n',
        b'bad-\xff.js': b'function original_js() { return original_python; }\n',
        b'bad-\xef\xbf\xbd.js': b'function replacement_js() { return replacement_python; }\n',
        b'nested-\xfe/a.py': b'def nested_original(): pass\n',
        b'nested-\xef\xbf\xbd/a.py': b'def nested_replacement(): pass\n',
        b'line\nquote"slash\\name.py': b'def unusual_ascii(): pass\n',
    }
    for name, contents in files.items():
        path = Path(os.fsdecode(os.fsencode(root)+b'/'+name))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    monkeypatch.delenv('RIPGREP_CONFIG_PATH', raising=False)
    if discovery == 'walk':
        monkeypatch.setenv('PATH', str(tmp_path/'missing-binaries'))
    options = {'include_tests': True}
    if selection == 'focus': options['focus'] = 'bad-'
    if selection == 'subdirectory': options['path'] = os.fsdecode(b'nested-\xfe')
    if selection == 'no_refs': options['include_refs'] = False
    reference = outline.scan(str(root), **options)
    children = observe_children(monkeypatch)
    actual = scanner(root, **options)
    assert actual.files == reference.files
    assert actual.tags == reference.tags
    assert actual.references == reference.references
    assert actual.definitions == reference.definitions
    if discovery == 'walk' and selection != 'subdirectory':
        assert actual.tags[os.fsdecode(b'bad-\xff.py')][0].name == 'original_python'
        assert actual.tags['bad-\ufffd.py'][0].name == 'replacement_python'
        assert actual.tags[os.fsdecode(b'bad-\xff.js')][0].name == 'original_js'
        assert actual.tags['bad-\ufffd.js'][0].name == 'replacement_js'
    assert len(children) == (1 if discovery == 'git' else 2)
    assert_joined(children)
