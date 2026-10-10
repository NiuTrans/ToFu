#!/usr/bin/env python3
"""Build a deterministic tofu-db release tarball plus a SHA-256 checksum manifest.

The release artifact is the stripped ``tofu-db`` binary plus the human release
manifest ``data/tofudb-release-v0.2.0-manifest.md``. ``Cargo.toml`` keeps
``strip = false`` because link-time ``--strip-all`` reproducibly zeroed
initialized ``.data`` (see README); the *validated distribution copy* is the
one place stripping is permitted, so this script strips a private copy of the
release binary and packages that, never the in-tree build output.

Determinism: the tarball normalizes every entry to a fixed uid/gid/uname/gname,
fixed mtime (the SOURCE_DATE_EPOCH of the binary's git commit, or an explicit
override), sorted member order, and gzip with a zeroed header mtime, so two
runs over the same inputs produce byte-identical output. The checksum manifest
records the SHA-256 of the tarball, the stripped binary, the unstripped binary,
and the release manifest, plus reproducible-build instructions.

Signing: pass ``--signing-key <secret-key.json>`` to also emit
``<base>.SHA256SUMS.sig`` — a detached Ed25519 signature over the exact
checksum-manifest bytes (``tofu-db.release-signature.v1`` JSON, secret half
zeroized after signing). Verification is the explicit operator gate
``tofu-db certify-release --manifest <SHA256SUMS> --signature <SHA256SUMS.sig>
--binary <packaged-tofu-db> [--pubkey <public-key.json>]``; without
``--signing-key`` the output stays the checksum manifest alone and release
certification remains blocked (fail-closed, matching docs/STORAGE.md).

Usage:
  release_package.py \
      --binary packages/tofu-db/target/release/tofu-db \
      --manifest data/tofudb-release-v0.2.0-manifest.md \
      --outdir data/release-v0.2.0 \
      [--version 0.2.0] [--source-date-epoch 1725960000] \
      [--signing-key <secret-key.json>]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Pin all metadata so the tarball is a pure function of file contents.
_FIXED_UID = 0
_FIXED_GID = 0
_FIXED_UNAME = "tofu"
_FIXED_GNAME = "tofu"
_BINARY_MODE = 0o755
_MANIFEST_MODE = 0o644


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit_epoch(repo_root: Path) -> int | None:
    """SOURCE_DATE_EPOCH from the binary's git commit; None when unavailable."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return int(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _strip_binary(source: Path, destination: Path) -> None:
    strip = shutil.which("strip")
    if strip is None:
        raise SystemExit("release packaging requires `strip` on PATH")
    shutil.copyfile(source, destination)
    subprocess.run([strip, "--strip-all", str(destination)], check=True)


def _normalized_tarinfo(name: str, size: int, mode: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = mode
    info.uid = _FIXED_UID
    info.gid = _FIXED_GID
    info.uname = _FIXED_UNAME
    info.gname = _FIXED_GNAME
    info.mtime = mtime
    info.type = tarfile.REGTYPE
    return info


def _write_deterministic_tarball(
    members: list[tuple[str, Path, int]], output: Path, mtime: int
) -> None:
    # gzip embeds the current time by default; write through GzipFile with a
    # zeroed mtime so the compressed bytes are reproducible too.
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for name, path, mode in sorted(members, key=lambda m: m[0]):
                    info = _normalized_tarinfo(name, path.stat().st_size, mode, mtime)
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)


def _sign_checksums(checksums: Path, signing_key: Path) -> Path:
    """Emit the detached Ed25519 signature document beside the manifest.

    Pure-Python Ed25519 (RFC 8032, SHA-512) keeps the packaging script
    dependency-free; verification remains the Rust gate
    ``tofu_db::release_signature::verify_release``.
    """
    import json

    document = json.loads(signing_key.read_text(encoding="utf-8"))
    if document.get("format") != "tofu-db.release-signing-secret.v1":
        raise SystemExit("signing key format marker mismatch")
    secret = bytes.fromhex(document["secret"])
    public = bytes.fromhex(document["public"])
    if len(secret) != 32 or len(public) != 32:
        raise SystemExit("signing key halves must be 32 raw bytes each")
    try:
        derived_public, signature = _ed25519_sign(checksums.read_bytes(), secret)
    finally:
        # Zeroize the secret half as soon as signing completes.
        secret = b"\x00" * 32
        del secret
    if derived_public != public:
        raise SystemExit("embedded public key does not match the secret key's verifying key")
    key_id = hashlib.sha256(b"tofu-db.release-key-identity.v1" + public).hexdigest()
    signature_path = checksums.with_name(checksums.name + ".sig")
    signature_path.write_text(
        json.dumps(
            {
                "format": "tofu-db.release-signature.v1",
                "key_id": key_id,
                "signature": signature.hex(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return signature_path


# --- RFC 8032 Ed25519 over SHA-512 (pure Python, bounded inputs) -------------

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = None  # recovered once at first use


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    if x % 2 != 0:
        x = _P - x
    return x


def _edwards_add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    (x1, y1), (x2, y2) = p, q
    denominator = pow(1 + _D * x1 * x2 * y1 * y2, _P - 2, _P)
    x3 = (x1 * y2 + x2 * y1) * denominator % _P
    denominator = pow(1 - _D * x1 * x2 * y1 * y2, _P - 2, _P)
    y3 = (y1 * y2 + x1 * x2) * denominator % _P
    return x3, y3


def _scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    bits = y | ((x & 1) << 255)
    return bits.to_bytes(32, "little")


def _hint(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _ed25519_sign(message: bytes, secret: bytes) -> tuple[bytes, bytes]:
    """Return (public_key, signature) for ``message`` under the 32-byte seed."""
    global _BX
    digest = hashlib.sha512(secret).digest()
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = digest[32:]
    if _BX is None:
        _BX = _xrecover(_BY)
    base = (_BX, _BY)
    public = _encode_point(_scalarmult(base, a))
    r = _hint(prefix + message) % _Q
    encoded_r = _encode_point(_scalarmult(base, r))
    s = (r + _hint(encoded_r + public + message) * a) % _Q
    return public, encoded_r + s.to_bytes(32, "little")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path, help="unstripped release binary")
    parser.add_argument("--manifest", required=True, type=Path, help="release manifest markdown")
    parser.add_argument("--outdir", required=True, type=Path, help="output directory")
    parser.add_argument("--version", default="0.2.0")
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=None,
        help="fixed mtime for tarball entries (default: git commit time of --binary's repo)",
    )
    parser.add_argument(
        "--signing-key",
        type=Path,
        default=None,
        help="tofu-db.release-signing-secret.v1 JSON; emits <base>.SHA256SUMS.sig",
    )
    args = parser.parse_args(argv)

    binary = args.binary.resolve()
    manifest = args.manifest.resolve()
    if not binary.is_file():
        raise SystemExit(f"binary not found: {binary}")
    if not manifest.is_file():
        raise SystemExit(f"manifest not found: {manifest}")

    repo_root = binary
    for candidate in binary.parents:
        if (candidate / ".git").exists():
            repo_root = candidate
            break
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = _git_commit_epoch(repo_root)
    if epoch is None:
        raise SystemExit(
            "could not determine SOURCE_DATE_EPOCH; pass --source-date-epoch explicitly"
        )

    args.outdir.mkdir(parents=True, exist_ok=True)
    version = args.version
    base = f"tofu-db-{version}"
    tarball = args.outdir / f"{base}.tar.gz"
    checksums = args.outdir / f"{base}.SHA256SUMS"

    with tempfile.TemporaryDirectory(prefix="tofudb-release-") as tmp:
        stripped = Path(tmp) / "tofu-db"
        _strip_binary(binary, stripped)

        members = [
            (f"{base}/tofu-db", stripped, _BINARY_MODE),
            (f"{base}/RELEASE-MANIFEST.md", manifest, _MANIFEST_MODE),
        ]
        _write_deterministic_tarball(members, tarball, epoch)

        stripped_sha = _sha256(stripped)

    lines = [
        f"# tofu-db {version} release checksums",
        f"# tarball entries normalized: uid/gid=0 uname/gname=tofu mtime={epoch} gzip mtime=0",
        f"{_sha256(tarball)}  {tarball.name}",
        f"{stripped_sha}  {base}/tofu-db (stripped, packaged)",
        f"{_sha256(binary)}  tofu-db (unstripped build output, {binary})",
        f"{_sha256(manifest)}  {manifest.name}",
        "",
        "## Reproducible build",
        "1. git checkout the commit recorded in data/tofudb-release-v0.2.0-manifest.md",
        "2. cd packages/tofu-db  (its .cargo/config.toml pins the devtoolset-8 toolchain)",
        "3. cargo build --release            # strip=false; link-time stripping is unsafe",
        "4. python3 packages/tofu-db/scripts/release_package.py --binary packages/tofu-db/target/release/tofu-db \\",
        "       --manifest data/tofudb-release-v0.2.0-manifest.md \\",
        f"       --outdir <outdir> --version {version} --source-date-epoch {epoch}",
        "5. sha256sum -c <this manifest> against the regenerated tarball",
        "",
        "Integrity is content-addressed via SHA-256 above; when a .SHA256SUMS.sig",
        "detached Ed25519 signature is present, `tofu-db certify-release` verifies",
        "manifest authenticity and the packaged binary digest against the release",
        "signing public key.",
    ]
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.signing_key is not None:
        signature = _sign_checksums(checksums, args.signing_key)
        print(f"signature={signature}")
    else:
        print("signature=none (unsigned; release certification stays blocked)")
    print(f"tarball={tarball}")
    print(f"tarball_sha256={_sha256(tarball)}")
    print(f"checksums={checksums}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
