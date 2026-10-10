"""Hermetic native archive integrity, deterministic output and publication gates."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import tempfile
import unittest

from scripts.build_native_runtime import SPEC, FORMAT, verify, write_archive


def members():
    files = {name: b'fixture' for name in SPEC['required_members']}
    files['bin/tofu-native'] = b'\x7fELFfixture'
    manifest = {'format': FORMAT, 'files': {
        name: {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
        for name, content in files.items()}}
    files['manifest.json'] = json.dumps(manifest).encode()
    return files


class NativeArchiveTests(unittest.TestCase):
    def test_reproducible_bytes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / 'one.tar.gz'
            second = Path(temporary) / 'two.tar.gz'
            write_archive(members(), first)
            write_archive(members(), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(verify(first, run=False)['format'], FORMAT)
            original = first.read_bytes()
            with self.assertRaises(FileExistsError):
                write_archive(members(), first)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual({p.name for p in first.parent.iterdir()},
                             {'one.tar.gz', 'two.tar.gz'})

    def test_tampered_binary_and_missing_required_member_fail_before_execution(self):
        for mode in ('tamper', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'bad.tar.gz'
                files = members()
                if mode == 'tamper':
                    files['bin/tofu-native'] += b'altered'
                else:
                    files.pop('README.txt')
                write_archive(files, path)
                with self.assertRaises(ValueError):
                    verify(path, run=False)

    def test_traversal_links_and_duplicate_members_are_rejected(self):
        for mode in ('traversal', 'link', 'duplicate'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'bad.tar.gz'
                with tarfile.open(path, 'w:gz') as archive:
                    name = '../escape' if mode == 'traversal' else 'repeated'
                    entry = tarfile.TarInfo(name)
                    if mode == 'link':
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = '/outside'
                    else:
                        entry.size = 1
                    archive.addfile(entry, None if mode == 'link' else io.BytesIO(b'x'))
                    if mode == 'duplicate':
                        archive.addfile(entry, io.BytesIO(b'x'))
                with self.assertRaises(ValueError):
                    verify(path, run=False)
                self.assertFalse((path.parent / 'escape').exists())

    def test_missing_manifest_inventory_cannot_admit_extra_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'extra.tar.gz'
            files = members()
            files['unexpected'] = b'unlisted'
            write_archive(files, path)
            with self.assertRaises(ValueError):
                verify(path, run=False)


if __name__ == '__main__':
    unittest.main()
