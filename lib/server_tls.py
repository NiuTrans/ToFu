"""Development TLS certificate ownership for the Quart/Hypercorn runtime."""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
from collections.abc import Callable, Mapping

from lib.runtime_paths import data_root as runtime_data_root


BootReporter = Callable[..., object]


def _noop_boot(_message: str, *_args: object) -> None:
    return None


def ensure_tls_certificates(
    certfile: str = '',
    keyfile: str = '',
    bind_host: str = '',
    *,
    data_root: str | None = None,
    environ: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
    boot: BootReporter | None = None,
) -> tuple[str, str]:
    """Reuse or create a local certificate pair for explicit direct TLS."""
    log = logger or logging.getLogger('server.tls')
    report = boot or _noop_boot
    env = os.environ if environ is None else environ

    if certfile and keyfile:
        if os.path.isfile(certfile) and os.path.isfile(keyfile):
            log.info('[TLS] Using provided certs: %s, %s', certfile, keyfile)
            return certfile, keyfile
        missing = [
            path for path in (certfile, keyfile) if not os.path.isfile(path)
        ]
        raise FileNotFoundError(
            'configured TLS file(s) not found: ' + ', '.join(missing)
        )

    cert_dir = os.path.join(data_root or runtime_data_root(), 'certs')
    cert_path = os.path.join(cert_dir, 'tofu.pem')
    key_path = os.path.join(cert_dir, 'tofu.key')
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        log.info('[TLS] Reusing existing self-signed certs at %s', cert_dir)
        return cert_path, key_path

    report('Generating self-signed TLS certificate for HTTP/2…')
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        os.makedirs(cert_dir, exist_ok=True)
        hostname = socket.gethostname()
        san_tokens = [
            'localhost', '127.0.0.1', '::1', hostname, socket.getfqdn(),
        ]
        if bind_host not in ('', '0.0.0.0', '::'):
            san_tokens.append(bind_host)
        san_tokens.extend(
            value.strip()
            for value in env.get('TOFU_TLS_SANS', '').split(',')
            if value.strip()
        )
        try:
            for info in socket.getaddrinfo(hostname, None):
                address = info[4][0]
                if address:
                    san_tokens.append(address)
        except OSError as exc:
            log.debug('[TLS] local-address discovery skipped: %s', exc)

        sans = []
        seen_sans: set[tuple[str, str]] = set()
        for raw_token in san_tokens:
            token = (raw_token or '').strip().rstrip('.')
            if not token or token in ('0.0.0.0', '::'):
                continue
            try:
                address = ipaddress.ip_address(token)
                identity = ('ip', str(address))
                san = x509.IPAddress(address)
            except ValueError as exc:
                log.debug('[TLS] treating SAN %r as a DNS name: %s', token, exc)
                identity = ('dns', token.lower())
                san = x509.DNSName(token)
            if identity in seen_sans:
                continue
            seen_sans.add(identity)
            sans.append(san)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f'Tofu Server ({hostname})'),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        descriptor = os.open(
            key_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(descriptor, key_pem)
        finally:
            os.close(descriptor)
        with open(cert_path, 'wb') as cert_file:
            cert_file.write(cert.public_bytes(serialization.Encoding.PEM))

        log.info(
            '[TLS] Generated self-signed cert at %s '
            '(valid 10 years; SAN=%s)',
            cert_dir,
            ','.join(value for value in san_tokens if value),
        )
        report('TLS certificate ready (self-signed, valid 10 years).')
        return cert_path, key_path
    except ImportError as exc:
        raise RuntimeError(
            'TLS was requested but cryptography is not installed; '
            'install it with: pip install cryptography') from exc
    except Exception as exc:
        raise RuntimeError(
            f'TLS certificate generation failed: {exc}') from exc


__all__ = ['ensure_tls_certificates']
