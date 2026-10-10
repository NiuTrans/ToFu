"""Content integrity checks before OCI archive publication."""
import hashlib

import pytest

from evaluations.swebench.images import _verify_oci_blobs

pytestmark = pytest.mark.unit


def blob_root(tmp_path):
    root = tmp_path / 'blobs' / 'sha256'
    root.mkdir(parents=True)
    return root


def test_accepts_content_addressed_blob(tmp_path):
    root = blob_root(tmp_path)
    payload = b'known image bytes'
    (root / hashlib.sha256(payload).hexdigest()).write_bytes(payload)
    _verify_oci_blobs(tmp_path)


def test_rejects_corrupt_blob(tmp_path):
    root = blob_root(tmp_path)
    (root / hashlib.sha256(b'expected').hexdigest()).write_bytes(b'corrupted')
    with pytest.raises(RuntimeError, match='digest mismatch'):
        _verify_oci_blobs(tmp_path)


def test_rejects_symlink(tmp_path):
    root = blob_root(tmp_path)
    target = tmp_path / 'target'
    target.write_bytes(b'data')
    (root / hashlib.sha256(b'data').hexdigest()).symlink_to(target)
    with pytest.raises(RuntimeError, match='regular'):
        _verify_oci_blobs(tmp_path)


def test_rejects_empty_directory(tmp_path):
    blob_root(tmp_path)
    with pytest.raises(RuntimeError, match='empty'):
        _verify_oci_blobs(tmp_path)
