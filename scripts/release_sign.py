#!/usr/bin/env python3
"""Sign and verify the Tofu release checksum manifest (SHA256SUMS).

WHY THIS FILE EXISTS
--------------------
A Tofu release today ships installers for Windows, macOS (two arches), Linux
(tarball + .deb) and the headless agent, plus a ``SHA256SUMS`` manifest that
``shasum -a 256 -c`` verifies. That manifest proves **integrity** — the bytes
you downloaded match the bytes the builder hashed. It does NOT prove
**authenticity**: anyone who can alter the release page (a compromised token,
a tampered mirror, a lookalike re-upload) can alter the installers AND
re-generate a matching ``SHA256SUMS``. The checksum then verifies cleanly
against the attacker's own manifest.

This module closes that gap with a single detached Ed25519 signature over the
canonical ``SHA256SUMS`` bytes:

  * ``SHA256SUMS``      — unchanged, still verified with ``shasum``;
  * ``SHA256SUMS.sig``  — 64-byte raw Ed25519 signature over ``SHA256SUMS``;
  * ``release_signing_public_key.pem`` — the SubjectPublicKeyInfo the
    signature verifies against, published next to the assets so a first-time
    user needs nothing but the release page.

WHY ED25519 OVER THE MANIFEST, NOT PER-INSTALLER AUTHENTICODE / NOTARIZATION
-----------------------------------------------------------------------------
OS-native signing (Authenticode, Apple notarization) is per-platform, needs a
paid certificate, and cannot run on this offline build host. The project's own
release notes currently tell users to bypass SmartScreen/Gatekeeper for
exactly that reason. What CAN be done everywhere — including Linux and
Android/Termux, which have no OS code-signing path at all — is a portable
signature over the one manifest that already covers every asset. A user (or
the packaging gate) downloads ``SHA256SUMS`` + ``SHA256SUMS.sig`` + the public
key and verifies the whole release with one offline check, then trusts the
per-asset checksums the manifest vouches for. That is the strongest claim
achievable without a per-platform CA, and it composes: when Authenticode /
notarization secrets are later provided, they add a second layer rather than
replace this one.

WHY PYTHON ``cryptography`` AND NOT gpg/minisign/cosign
-------------------------------------------------------
The verify path must run in CI, in the release workflow, and on a fresh user
machine with zero extra tooling. ``cryptography`` is already a pinned project
dependency (it backs the rest of Tofu's crypto), so the verifier imports it
instead of shelling out to a binary that may not exist on the target. Ed25519
gives 64-byte signatures and 32-byte keys — small enough to embed, fast enough
that verification is never the bottleneck, and deterministic (no RNG misuse
mode).

THREE MODES, THREE EXIT CODE SETS
---------------------------------
``keygen``  — generate a keypair. Refuses to overwrite an existing key
              unless ``--force`` (rotating a signing key is a deliberate,
              loud act, never an accident).
``sign``    — produce ``SHA256SUMS.sig`` from a private key.
``verify``  — fail-closed check that ``SHA256SUMS.sig`` matches ``SHA256SUMS``
              under a public key. Any mismatch, truncation, malformed key, or
              missing file is a non-zero exit with a named reason; there is no
              "unsigned but okay" path.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
        load_pem_public_key,
    )
except ImportError as exc:  # pragma: no cover - dependency is pinned in CI
    print(f'FATAL — the cryptography package is required: {exc}', file=sys.stderr)
    raise SystemExit(2)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# PEM armor for the public key that ships beside the assets. PEM (not raw
# 32-byte) so `openssl pkey -pubin -text` can also inspect it, and so a user
# can pin it out-of-band. The signature itself is raw 64-byte Ed25519 — the
# smallest unambiguous form, with no container format to parse.
PUBLIC_KEY_NAME = 'release_signing_public_key.pem'
SIGNATURE_SUFFIX = '.sig'
_ED25519_SIGNATURE_BYTES = 64


def _load_private_key(path: Path, password: bytes | None) -> Ed25519PrivateKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f'cannot read private key {path}: {exc}')
    try:
        key = load_pem_private_key(raw, password=password)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{path} is not a loadable PEM private key: {exc}')
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f'{path} is not an Ed25519 private key')
    return key


def _load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f'cannot read public key {path}: {exc}')
    try:
        key = load_pem_public_key(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{path} is not a loadable PEM public key: {exc}')
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f'{path} is not an Ed25519 public key')
    return key


def cmd_keygen(private_path: Path, public_path: Path, *, force: bool) -> int:
    for target in (private_path, public_path):
        if target.exists() and not force:
            print(f'REFUSED — {target} already exists; rotating a release '
                  'signing key is a deliberate act. Re-run with --force to '
                  'overwrite (this invalidates every signature made with the '
                  'old key).', file=sys.stderr)
            return EXIT_FAILED
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    # Owner-readonly: a private signing key must never be group/world visible.
    private_path.chmod(0o600)
    public_path.write_bytes(key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    print(f'OK — wrote private key {private_path} (mode 0600) and public key '
          f'{public_path}.')
    print(f'Fingerprint (SHA-256 of the raw 32-byte public key): '
          f'{public_key_fingerprint(key.public_key())}')
    return EXIT_OK


def public_key_fingerprint(key: Ed25519PublicKey) -> str:
    """Stable, shareable fingerprint of the raw public key bytes."""
    import hashlib
    raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode('ascii')


def cmd_sign(manifest: Path, private_path: Path, out: Path) -> int:
    try:
        document = manifest.read_bytes()
    except OSError as exc:
        print(f'FAILED — cannot read manifest {manifest}: {exc}', file=sys.stderr)
        return EXIT_FAILED
    if not document:
        print(f'FAILED — manifest {manifest} is empty; nothing to sign.',
              file=sys.stderr)
        return EXIT_FAILED
    try:
        key = _load_private_key(private_path, password=None)
    except ValueError as exc:
        print(f'FAILED — {exc}', file=sys.stderr)
        return EXIT_FAILED
    signature = key.sign(document)
    out.write_bytes(signature)
    print(f'OK — signed {manifest} ({len(document)} bytes) -> {out} '
          f'({len(signature)} bytes).')
    print(f'Public-key fingerprint: {public_key_fingerprint(key.public_key())}')
    return EXIT_OK


def cmd_verify(manifest: Path, signature_path: Path, public_path: Path) -> int:
    """Fail-closed verification. Returns EXIT_OK only on a proven match."""
    try:
        document = manifest.read_bytes()
    except OSError as exc:
        print(f'FAILED — cannot read manifest {manifest}: {exc}', file=sys.stderr)
        return EXIT_FAILED
    try:
        signature = signature_path.read_bytes()
    except OSError as exc:
        print(f'FAILED — cannot read signature {signature_path}: {exc}',
              file=sys.stderr)
        return EXIT_FAILED
    if len(signature) != _ED25519_SIGNATURE_BYTES:
        print(f'FAILED — {signature_path} is {len(signature)} bytes, not the '
              f'{_ED25519_SIGNATURE_BYTES}-byte Ed25519 signature; the file is '
              'truncated or not a raw signature.', file=sys.stderr)
        return EXIT_FAILED
    try:
        key = _load_public_key(public_path)
    except ValueError as exc:
        print(f'FAILED — {exc}', file=sys.stderr)
        return EXIT_FAILED
    try:
        key.verify(signature, document)
    except Exception:
        print(f'FAILED — signature {signature_path} does NOT verify against '
              f'{manifest} under {public_path}. The manifest or signature was '
              'altered, or the wrong public key was used.', file=sys.stderr)
        return EXIT_FAILED
    print(f'OK — {manifest} is authentic under {public_path} '
          f'({public_key_fingerprint(key)}).')
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='Sign / verify the Tofu release SHA256SUMS manifest.')
    sub = ap.add_subparsers(dest='command', required=True)

    kg = sub.add_parser('keygen', help='generate an Ed25519 release keypair')
    kg.add_argument('--private', required=True, metavar='PATH')
    kg.add_argument('--public', required=True, metavar='PATH')
    kg.add_argument('--force', action='store_true',
                    help='overwrite existing keys (invalidates old signatures)')

    sg = sub.add_parser('sign', help='sign a manifest with a private key')
    sg.add_argument('--manifest', required=True, metavar='PATH',
                    help='the SHA256SUMS file to sign')
    sg.add_argument('--private', required=True, metavar='PATH')
    sg.add_argument('--out', required=True, metavar='PATH',
                    help='where to write the raw signature (e.g. SHA256SUMS.sig)')

    vf = sub.add_parser('verify', help='verify a manifest against a signature')
    vf.add_argument('--manifest', required=True, metavar='PATH')
    vf.add_argument('--signature', required=True, metavar='PATH')
    vf.add_argument('--public', required=True, metavar='PATH')

    args = ap.parse_args(argv)
    if args.command == 'keygen':
        return cmd_keygen(Path(args.private), Path(args.public), force=args.force)
    if args.command == 'sign':
        return cmd_sign(Path(args.manifest), Path(args.private), Path(args.out))
    return cmd_verify(Path(args.manifest), Path(args.signature), Path(args.public))


if __name__ == '__main__':
    raise SystemExit(main())
