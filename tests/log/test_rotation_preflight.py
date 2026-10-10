"""No-rotation preflight preserves validation, exact thresholds and side effects."""
import os
from pathlib import Path
import pytest
from lib import log_retention

pytestmark = pytest.mark.unit


def test_small_string_file_keeps_caller_result_content_inode_and_mode(tmp_path):
    file = tmp_path/'audit.log';file.write_bytes(b'owned\n');os.chmod(file,0o640)
    before=file.stat();result={'rotated':[],'removed':[],'errors':[],'sentinel':'owned'}
    assert log_retention.copytruncate_if_oversize(str(file),max_bytes=64,backup_count=2,result=result) is result
    assert file.read_bytes()==b'owned\n' and file.stat().st_ino==before.st_ino
    assert file.stat().st_mode==before.st_mode and result['rotated']==[]


def test_exact_trigger_is_not_rotated_but_next_byte_uses_full_rotation(tmp_path):
    file=tmp_path/'audit.log';file.write_bytes(b'line\n')
    assert not log_retention.copytruncate_if_oversize(str(file),max_bytes=16,trigger_bytes=5,backup_count=1)['rotated']
    file.write_bytes(b'line\nx\n')
    result=log_retention.copytruncate_if_oversize(str(file),max_bytes=16,trigger_bytes=5,backup_count=1)
    assert result['rotated'] and file.read_bytes()==b''
    assert (tmp_path/'audit.log.1').read_bytes()==b'line\nx\n'


def test_symlink_never_rotates_external_file(tmp_path):
    external=tmp_path/'external';external.write_bytes(b'outside\n')
    link=tmp_path/'audit.log';link.symlink_to(external)
    assert not log_retention.copytruncate_if_oversize(str(link),max_bytes=1,backup_count=1)['rotated']
    assert external.read_bytes()==b'outside\n' and not (tmp_path/'audit.log.1').exists()


@pytest.mark.skipif(not hasattr(os,'mkfifo'),reason='FIFO is a POSIX file type')
def test_fifo_preflight_is_nonblocking_and_does_not_open_it(tmp_path):
    fifo=tmp_path/'audit.log';os.mkfifo(fifo)
    assert not log_retention.copytruncate_if_oversize(str(fifo),max_bytes=1,backup_count=1)['rotated']


def test_custom_numeric_value_retains_single_conversion_and_full_behavior(tmp_path):
    file=tmp_path/'audit.log';file.write_bytes(b'line\n');calls=[]
    class Ceiling:
        def __int__(self):
            calls.append('converted');return 64
    assert not log_retention.copytruncate_if_oversize(str(file),max_bytes=Ceiling(),backup_count=1)['rotated']
    assert calls==['converted']


def test_invalid_backup_argument_still_raises_on_small_file(tmp_path):
    file=tmp_path/'audit.log';file.write_bytes(b'line\n')
    with pytest.raises(ValueError):
        log_retention.copytruncate_if_oversize(str(file),max_bytes=64,backup_count='invalid')


def test_missing_file_still_validates_numeric_arguments(tmp_path):
    with pytest.raises(ValueError):
        log_retention.copytruncate_if_oversize(str(tmp_path/'absent'),max_bytes='invalid',backup_count=1)


def test_dry_run_oversize_preserves_original_file_and_reports_backup_plan(tmp_path):
    file=tmp_path/'audit.log';file.write_bytes(b'first\nsecond\n');before=file.read_bytes()
    result=log_retention.copytruncate_if_oversize(str(file),max_bytes=64,trigger_bytes=1,backup_count=1,dry_run=True)
    assert result['rotated'] and result['rotated'][0]['dry_run']
    assert file.read_bytes()==before and not (tmp_path/'audit.log.1').exists()


def test_string_rotation_rejects_replacement_symlink_before_open(tmp_path, monkeypatch):
    file=tmp_path/'audit.log';file.write_bytes(b'owned-long-line\n')
    external=tmp_path/'outside';external.write_bytes(b'untouched\n')
    real_open=log_retention._open_log
    def replace(path, flags, mode=log_retention.LOG_FILE_MODE):
        if str(path)==str(file):
            file.unlink();file.symlink_to(external)
        return real_open(path, flags, mode)
    monkeypatch.setattr(log_retention,'_open_log',replace)
    result=log_retention.copytruncate_if_oversize(str(file),max_bytes=8,backup_count=1)
    assert not result['rotated'] and result['errors']
    assert external.read_bytes()==b'untouched\n'


def test_string_rotation_rejects_different_inode_at_open(tmp_path, monkeypatch):
    file=tmp_path/'audit.log';file.write_bytes(b'owned-long-line\n')
    old=tmp_path/'old';replacement=b'replacement-keep\n';real_open=log_retention._open_log
    def replace(path,flags,mode=log_retention.LOG_FILE_MODE):
        if str(path)==str(file):
            file.rename(old);file.write_bytes(replacement)
        return real_open(path,flags,mode)
    monkeypatch.setattr(log_retention,'_open_log',replace)
    result=log_retention.copytruncate_if_oversize(str(file),max_bytes=8,backup_count=1)
    assert not result['rotated'] and file.read_bytes()==replacement
    assert old.read_bytes()==b'owned-long-line\n'
