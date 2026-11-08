"""In-memory file tree and the deterministic archive writer (README §2).

Entry points: ``Tree`` (entries keyed by archive path, with symlink-aware
parent resolution for merged-/usr userlands), ``Tree.import_tar`` and
``Tree.import_dir`` (sources), ``write_archive`` (tar + ``.manifest.json`` +
``gzip -n -9``), ``canonical_json``. Content lives in files on disk: either
the staged source file itself or a content-addressed copy in a blob store.
Dependencies: stdlib, the ``gzip`` executable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile

MANIFEST_NAME = '.manifest.json'
MANIFEST_FORMAT = 1
# A gzip stream named .tgz, never .gz: the Android Gradle Plugin's asset merger
# un-gzips every *.gz asset (sdk-common AssetItem.shouldBeUnGzipped), which
# would break the index's file name and SHA-256 (contract v1.1).
ARCHIVE_SUFFIX = '.tgz'
ENTRY_TYPES = ('file', 'dir', 'symlink', 'hardlink')
VERSION_DIGEST_HEX = 12
# Linux's own bound on symlinks followed during one path resolution
# (MAXSYMLINKS in fs/namei.c).
SYMLINK_HOPS = 40
_READ_CHUNK = 1024 * 1024


class TreeError(RuntimeError):
    """A source cannot be represented under the archive contract."""


def canonical_json(value) -> str:
    """Compact, key-sorted JSON: the one serialization of manifests and digests."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass
class Entry:
    path: str
    type: str
    mode: int
    target: str = ''
    source: Path | None = None
    size: int = 0
    sha256: str = ''

    def manifest_row(self) -> dict:
        row = {'path': self.path, 'type': self.type, 'mode': f'{self.mode & 0o7777:04o}'}
        if self.type in ('file', 'hardlink'):
            row['size'] = self.size
            row['sha256'] = self.sha256
        if self.type in ('symlink', 'hardlink'):
            row['target'] = self.target
        return row


def normalize_path(path: str) -> str:
    """Archive path form: relative, '/'-separated, no '.', '..' or empty parts."""
    text = path.replace('\\', '/')
    while text.startswith('./'):
        text = text[2:]
    text = text.strip('/')
    parts = [part for part in text.split('/') if part not in ('', '.')]
    if not parts or '..' in parts:
        raise TreeError(f'unsafe archive path {path!r}')
    return '/'.join(parts)


class BlobStore:
    """Content-addressed copies of streamed file content (one per digest)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put_stream(self, stream) -> tuple[Path, str, int]:
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as handle:
            for chunk in iter(lambda: stream.read(_READ_CHUNK), b''):
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
        temporary = Path(handle.name)
        final = self.root / digest.hexdigest()
        if final.exists():
            temporary.unlink()
        else:
            os.replace(temporary, final)
        return final, digest.hexdigest(), size

    def put_bytes(self, data: bytes) -> tuple[Path, str, int]:
        final = self.root / hashlib.sha256(data).hexdigest()
        if not final.exists():
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as handle:
                handle.write(data)
            os.replace(handle.name, final)
        return final, final.name, len(data)


class Tree:
    """Archive entries keyed by path. Parents are directory entries."""

    def __init__(self, blobs: BlobStore) -> None:
        self.blobs = blobs
        self.entries: dict[str, Entry] = {}

    # ── lookup ──────────────────────────────────────────────────────────
    def get(self, path: str) -> Entry | None:
        return self.entries.get(path)

    def resolve_parent(self, path: str) -> str:
        """Follow directory symlinks in the parent chain, as dpkg does.

        ``lib/aarch64-linux-gnu/x`` becomes ``usr/lib/aarch64-linux-gnu/x``
        when ``lib`` is a symlink to ``usr/lib``. The last component is not
        followed. Targets resolve inside the tree: absolute ones from its root.
        """
        parts = normalize_path(path).split('/')
        pending = parts[:-1]
        resolved: list[str] = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part == '..':
                if resolved:
                    resolved.pop()
                continue
            entry = self.entries.get('/'.join([*resolved, part]))
            if entry is not None and entry.type == 'symlink':
                hops += 1
                if hops > SYMLINK_HOPS:
                    raise TreeError(f'symlink loop resolving {path}')
                if entry.target.startswith('/'):
                    resolved = []
                pending = [piece for piece in entry.target.split('/')
                           if piece not in ('', '.')] + pending
                continue
            resolved.append(part)
        return '/'.join([*resolved, parts[-1]])

    # ── mutation ────────────────────────────────────────────────────────
    def _ensure_parents(self, path: str) -> None:
        parent = PurePosixPath(path).parent
        missing = []
        while str(parent) not in ('.', ''):
            entry = self.entries.get(str(parent))
            if entry is not None:
                if entry.type != 'dir':
                    raise TreeError(f'{path}: parent {parent} is a {entry.type}')
                break
            missing.append(str(parent))
            parent = parent.parent
        for directory in reversed(missing):
            self.entries[directory] = Entry(directory, 'dir', 0o755)

    def _place(self, entry: Entry, *, replace: bool) -> None:
        existing = self.entries.get(entry.path)
        if existing is not None:
            if existing.type == 'dir' and entry.type == 'dir':
                existing.mode = entry.mode
                return
            if not replace:
                raise TreeError(f'{entry.path}: already present as {existing.type}')
            if existing.type == 'dir':
                self.remove(entry.path)
        self._ensure_parents(entry.path)
        self.entries[entry.path] = entry

    def add_dir(self, path: str, mode: int = 0o755, *, replace: bool = False) -> None:
        self._place(Entry(normalize_path(path), 'dir', mode), replace=replace)

    def add_file(self, path: str, mode: int, source: Path, *, sha256: str = '',
                 size: int = -1, replace: bool = False) -> None:
        if not sha256 or size < 0:
            sha256, size = sha256_file(source)
        self._place(Entry(normalize_path(path), 'file', mode, source=source,
                          size=size, sha256=sha256), replace=replace)

    def add_bytes(self, path: str, data: bytes, mode: int = 0o644, *,
                  replace: bool = False) -> None:
        source, digest, size = self.blobs.put_bytes(data)
        self.add_file(path, mode, source, sha256=digest, size=size, replace=replace)

    def add_symlink(self, path: str, target: str, *, replace: bool = False) -> None:
        if not target:
            raise TreeError(f'{path}: empty symlink target')
        self._place(Entry(normalize_path(path), 'symlink', 0o777, target=target),
                    replace=replace)

    def add_hardlink(self, path: str, target: str, *, replace: bool = False) -> None:
        target = normalize_path(target)
        origin = self.entries.get(target)
        if origin is None or origin.type not in ('file', 'hardlink'):
            raise TreeError(f'{path}: hard link target {target} is not a regular file')
        if origin.type == 'hardlink':
            target = origin.target
            origin = self.entries[target]
        self._place(Entry(normalize_path(path), 'hardlink', origin.mode, target=target,
                          source=origin.source, size=origin.size, sha256=origin.sha256),
                    replace=replace)

    def remove(self, path: str) -> None:
        path = normalize_path(path)
        prefix = path + '/'
        for key in [key for key in self.entries if key == path or key.startswith(prefix)]:
            del self.entries[key]

    def ensure_dir(self, path: str, mode: int = 0o755) -> None:
        path = normalize_path(path)
        if path not in self.entries:
            self.add_dir(path, mode)

    # ── sources ─────────────────────────────────────────────────────────
    def import_tar(self, archive: tarfile.TarFile, *, prefix: str = '',
                   follow_parent_links: bool = False, replace: bool = False,
                   skip=None) -> list[str]:
        """Import every member with its exact mode; return the archive paths.

        ``follow_parent_links`` places members through directory symlinks
        already in the tree (dpkg semantics); a directory member that lands
        on a symlink to a directory keeps the symlink. ``skip(path, member)``
        drops a member from the tree; its path is still returned.
        """
        imported: list[str] = []
        for member in archive:
            raw = member.name
            stripped = raw[2:] if raw.startswith('./') else raw
            if stripped.strip('/') in ('', '.'):
                continue
            name = normalize_path(f'{prefix}/{stripped}' if prefix else stripped)
            imported.append(name)
            if skip is not None and skip(name, member):
                continue
            if follow_parent_links:
                name = self.resolve_parent(name)
            mode = member.mode & 0o7777
            if member.isdir():
                existing = self.entries.get(name)
                if existing is not None and existing.type == 'symlink':
                    continue
                self.add_dir(name, mode, replace=replace)
            elif member.isfile():
                stream = archive.extractfile(member)
                source, digest, size = self.blobs.put_stream(stream)
                self.add_file(name, mode, source, sha256=digest, size=size, replace=replace)
            elif member.issym():
                self.add_symlink(name, member.linkname, replace=replace)
            elif member.islnk():
                link = member.linkname[2:] if member.linkname.startswith('./') else member.linkname
                target = normalize_path(f'{prefix}/{link}' if prefix else link)
                if follow_parent_links:
                    target = self.resolve_parent(target)
                self.add_hardlink(name, target, replace=replace)
            else:
                raise TreeError(f'{raw}: unsupported tar member type {member.type!r}')
        return imported

    def import_dir(self, root: Path, *, prefix: str, file_mode=None,
                   skip=None) -> None:
        """Import a staged directory; modes are normalized by ``file_mode``.

        ``file_mode(path, st_mode)`` returns the mode to record; by default
        directories are 0755, files 0755 when any execute bit is set, else
        0644. Hard links on disk become hardlink entries.
        """
        seen_inodes: dict[tuple[int, int], str] = {}
        for directory, dirnames, filenames in os.walk(root):
            dirnames.sort()
            relative_dir = Path(directory).relative_to(root).as_posix()
            for name in sorted(dirnames + filenames):
                relative = name if relative_dir == '.' else f'{relative_dir}/{name}'
                if skip is not None and skip(relative):
                    if name in dirnames:
                        dirnames.remove(name)
                    continue
                full = Path(directory) / name
                status = full.lstat()
                path = f'{prefix}/{relative}' if prefix else relative
                if stat.S_ISLNK(status.st_mode):
                    self.add_symlink(path, os.readlink(full))
                elif stat.S_ISDIR(status.st_mode):
                    self.add_dir(path, 0o755)
                elif stat.S_ISREG(status.st_mode):
                    mode = (file_mode(relative, status.st_mode) if file_mode is not None
                            else (0o755 if status.st_mode & 0o111 else 0o644))
                    key = (status.st_dev, status.st_ino)
                    if status.st_nlink > 1 and key in seen_inodes:
                        self.add_hardlink(path, seen_inodes[key])
                        continue
                    seen_inodes[key] = normalize_path(path)
                    self.add_file(path, mode, full)
                else:
                    raise TreeError(f'{full}: unsupported file type')

    # ── output ──────────────────────────────────────────────────────────
    def ordered(self) -> list[Entry]:
        """Entries in archive order. Hard links are written as regular files.

        Android apps cannot create hard links (AOSP sepolicy neverallow on
        ``file link`` for untrusted apps), so every path of a hard-link set
        becomes its own copy of the content (contract v1.1).
        """
        ordered = sorted(self.entries.values(), key=lambda entry: entry.path.encode('utf-8'))
        for entry in ordered:
            if entry.type == 'hardlink':
                entry.type, entry.target = 'file', ''
        for entry in ordered:
            parent = str(PurePosixPath(entry.path).parent)
            if parent != '.':
                holder = self.entries.get(parent)
                if holder is None or holder.type != 'dir':
                    raise TreeError(f'{entry.path}: parent is not a directory entry')
        return ordered


def manifest_rows(entries: list[Entry]) -> list[dict]:
    return [entry.manifest_row() for entry in entries]


def version_for(label: str, body: dict) -> str:
    """``<label>-<digest>`` over the manifest without its version field.

    The manifest pins every entry's metadata and content digest, and the tar
    is a pure function of it, so equal versions mean byte-identical archives.
    """
    digest = hashlib.sha256(canonical_json(body).encode('utf-8')).hexdigest()
    return f'{label}-{digest[:VERSION_DIGEST_HEX]}'


class _CountingWriter:
    def __init__(self, sink) -> None:
        self.sink = sink
        self.count = 0

    def write(self, data) -> int:
        self.count += len(data)
        self.sink.write(data)
        return len(data)

    def flush(self) -> None:
        self.sink.flush()


def _tarinfo(path: str, kind: str, mode: int, *, size: int = 0, target: str = '') -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.mode = mode & 0o7777
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    info.mtime = 0
    info.size = 0
    if kind == 'dir':
        info.type = tarfile.DIRTYPE
    elif kind == 'file':
        info.type = tarfile.REGTYPE
        info.size = size
    elif kind == 'symlink':
        info.type = tarfile.SYMTYPE
        info.linkname = target
    elif kind == 'hardlink':
        info.type = tarfile.LNKTYPE
        info.linkname = target
    else:
        raise TreeError(f'{path}: unknown entry type {kind}')
    return info


def write_tar_stream(sink, entries: list[Entry], manifest: bytes | None) -> int:
    """Write the deterministic pax tar; return the uncompressed byte count."""
    counter = _CountingWriter(sink)
    with tarfile.open(fileobj=counter, mode='w|', format=tarfile.PAX_FORMAT,
                      encoding='utf-8') as archive:
        if manifest is not None:
            info = _tarinfo(MANIFEST_NAME, 'file', 0o644, size=len(manifest))
            archive.addfile(info, _BytesReader(manifest))
        for entry in entries:
            info = _tarinfo(entry.path, entry.type, entry.mode, size=entry.size,
                            target=entry.target)
            if entry.type == 'file':
                with open(entry.source, 'rb') as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)
    counter.flush()
    return counter.count


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk


@dataclass
class ArchiveResult:
    kind: str
    version: str
    path: Path
    sha256: str
    size: int
    tar_size: int
    manifest: dict


def gzip_command() -> list[str]:
    gzip = shutil.which('gzip')
    if gzip is None:
        raise TreeError('the gzip executable is required (gzip -n -9)')
    return [gzip, '-n', '-9']


def write_archive(tree: Tree, *, kind: str, abi: str, label: str, inputs: dict,
                  out_dir: Path) -> ArchiveResult:
    """Write ``<kind>-<version>.tgz`` with its manifest first."""
    entries = tree.ordered()
    body = {
        'abi': abi, 'entries': manifest_rows(entries), 'format': MANIFEST_FORMAT,
        'inputs': inputs, 'kind': kind,
    }
    version = version_for(label, body)
    manifest = {**body, 'version': version}
    manifest_bytes = (canonical_json(manifest) + '\n').encode('utf-8')
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'{kind}-{version}{ARCHIVE_SUFFIX}'
    partial = path.with_name(path.name + '.partial')
    with open(partial, 'wb') as output:
        process = subprocess.Popen(gzip_command(), stdin=subprocess.PIPE, stdout=output)
        try:
            tar_size = write_tar_stream(process.stdin, entries, manifest_bytes)
        finally:
            process.stdin.close()
            status = process.wait()
    if status != 0:
        raise TreeError(f'gzip failed with status {status}')
    os.replace(partial, path)
    digest, size = sha256_file(path)
    return ArchiveResult(kind, version, path, digest, size, tar_size, manifest)
