"""ca-certificates' configure step reproduced on the build host (README §5)."""
import shutil
import subprocess

import pytest

from runtime_builder import rootfs
from runtime_builder.tree import BlobStore, Tree

pytestmark = pytest.mark.skipif(shutil.which('openssl') is None, reason='needs openssl')


def _certificate(tmp_path, subject):
    key, cert = tmp_path / f'{subject}.key', tmp_path / f'{subject}.crt'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                    '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', f'/CN={subject}'], check=True, capture_output=True)
    return cert.read_bytes()


def test_links_bundle_and_configuration_match_update_ca_certificates(tmp_path):
    names = {'mozilla/Alpha_Root.crt': 'Alpha Root', 'mozilla/Beta (Old), CA.crt': 'Beta',
             'mozilla/Gamma.crt': 'Gamma'}
    tree = Tree(BlobStore(tmp_path / 'blobs'))
    tree.add_dir('usr/local/share', 0o755)
    contents = {}
    for name, subject in names.items():
        content = _certificate(tmp_path, subject.replace(' ', '_'))
        contents[name] = content.rstrip(b'\n') if name.endswith('Gamma.crt') else content
        tree.add_bytes(f'usr/share/ca-certificates/{name}', contents[name])
    rootfs.configure_ca_certificates(tree)

    def read(path):
        with open(tree.get(path).source, 'rb') as handle:
            return handle.read()

    configuration = read('etc/ca-certificates.conf').decode()
    assert configuration.startswith(rootfs.CA_CONF_HEADER)
    listed = configuration[len(rootfs.CA_CONF_HEADER):].splitlines()
    assert listed == sorted(names, key=str.encode)
    expected_bundle = b''.join(contents[name] if contents[name].endswith(b'\n')
                               else contents[name] + b'\n' for name in listed)
    assert read('etc/ssl/certs/ca-certificates.crt') == expected_bundle
    pem = tree.get('etc/ssl/certs/Beta_=Old=__CA.pem')
    assert pem.type == 'symlink' and pem.target == '/usr/share/ca-certificates/mozilla/Beta (Old), CA.crt'
    assert tree.get('usr/local/share/ca-certificates').type == 'dir'

    # openssl rehash over the same files must produce the same hash links.
    certs = tmp_path / 'certs'
    certs.mkdir()
    mine = {}
    for path, entry in tree.entries.items():
        if not path.startswith('etc/ssl/certs/') or entry.type != 'symlink':
            continue
        name = path.rsplit('/', 1)[1]
        if name.endswith('.pem'):
            (certs / name).write_bytes(read(entry.target.lstrip('/')))
        else:
            mine[name] = entry.target
    subprocess.run(['openssl', 'rehash', str(certs)], check=True, capture_output=True)
    theirs = {link.name: str(link.readlink()) for link in certs.iterdir() if link.is_symlink()}
    assert mine == theirs and len(mine) == 3
