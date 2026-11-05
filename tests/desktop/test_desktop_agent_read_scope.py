"""Device-side read scope: desktop file reads and listings stay inside the
declared shared folders on every permission tier, and the clipboard needs the
GUI tier (docs/modules/remote_execution.md, "Permissions")."""
import json
from unittest import mock

import pytest

from lib.desktop_agent import _dispatch as disp
from lib.desktop_agent import _gui
from lib.desktop_agent._permissions import build_permissions, safe_default

pytestmark = pytest.mark.unit


@pytest.fixture
def shared(tmp_path, monkeypatch):
    root = tmp_path / 'shared'
    root.mkdir()
    (root / 'notes.txt').write_text('shared-notes\n', encoding='utf-8')
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'id_rsa').write_text('PRIVATE-KEY-MATERIAL\n', encoding='utf-8')
    (root / 'escape').symlink_to(private, target_is_directory=True)
    (root / 'key-link').symlink_to(private / 'id_rsa')
    cfg = tmp_path / 'cfg.json'
    cfg.write_text(json.dumps({'share_roots': [{'name': 'shared', 'path': str(root)}]}),
                   encoding='utf-8')
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(cfg))
    monkeypatch.setenv('HOME', str(tmp_path))  # '~' lies outside the shared folder
    return root, private, cfg


@pytest.mark.parametrize('permissions', [safe_default(), build_permissions(allow_all=True)],
                         ids=['read-only-default', 'all-tiers'])
def test_file_reads_and_listings_stay_inside_shared_folders(shared, permissions):
    root, private, _ = shared
    read = disp.dispatch_command('desktop_read_file', {'path': str(root / 'notes.txt')}, permissions)
    assert read['content'] == 'shared-notes\n', read
    listing = disp.dispatch_command('desktop_list_files', {'path': str(root)}, permissions)
    assert 'notes.txt' in [entry['name'] for entry in listing['entries']], listing
    for command, path in [
            ('desktop_read_file', str(private / 'id_rsa')),
            ('desktop_read_file', str(root / 'key-link')),
            ('desktop_read_file', str(root / 'escape' / 'id_rsa')),
            ('desktop_read_file', str(root / '..' / 'private' / 'id_rsa')),
            ('desktop_read_file', '~/private/id_rsa'),
            ('desktop_list_files', str(private)),
            ('desktop_list_files', str(root / 'escape')),
            ('desktop_list_files', '~'),
            ('desktop_list_files', '/')]:
        out = disp.dispatch_command(command, {'path': path}, permissions)
        assert 'shared folders' in out.get('error', ''), (command, path, out)
        assert 'PRIVATE-KEY-MATERIAL' not in json.dumps(out) and 'entries' not in out


def test_no_shared_folders_means_no_local_file_reads(shared):
    root, _, cfg = shared
    cfg.write_text(json.dumps({'share_roots': []}), encoding='utf-8')
    for command, path in (('desktop_read_file', str(root / 'notes.txt')),
                          ('desktop_list_files', str(root))):
        out = disp.dispatch_command(command, {'path': path}, safe_default())
        assert 'share_roots' in out.get('error', ''), out


def test_clipboard_requires_the_gui_tier(monkeypatch):
    clipboard = mock.Mock()
    clipboard.paste.return_value = 'copied-password'
    monkeypatch.setattr(_gui, 'pyperclip', clipboard)
    for action in ('read', 'write'):
        out = disp.dispatch_command('desktop_clipboard', {'action': action, 'content': 'x'},
                                    build_permissions(allow_write=True, allow_exec=True))
        assert 'allow-gui' in out.get('error', ''), out
    clipboard.paste.assert_not_called()
    clipboard.copy.assert_not_called()
    assert disp.dispatch_command('desktop_clipboard', {'action': 'read'},
                                 build_permissions(allow_gui=True)) == {'content': 'copied-password'}
