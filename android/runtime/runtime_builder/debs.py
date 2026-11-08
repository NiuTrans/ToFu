"""Ubuntu archive metadata, dependency closure and dpkg registration (README §5).

Entry points: ``parse_deb822``, ``compare_versions`` (Debian policy §5.6.12),
``verify_inrelease``, ``load_packages_index``, ``resolve_closure`` (used by
``resolve`` to pin packages), ``install_deb`` (unpack one pinned .deb into a
``Tree`` and register it in the dpkg database as dpkg would), and
``write_status``. Dependencies: stdlib, ``gpgv`` and ``dpkg-deb``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import io
import lzma
import re
import subprocess
import tarfile

from .tree import Tree, TreeError

DPKG_STATUS = 'var/lib/dpkg/status'
DPKG_INFO = 'var/lib/dpkg/info'
# Fields dpkg writes into the status file, in dpkg's own order
# (lib/dpkg/parse.c fieldinfos[]); any other control field follows.
STATUS_FIELD_ORDER = (
    'Package', 'Essential', 'Protected', 'Status', 'Priority', 'Section',
    'Installed-Size', 'Origin', 'Maintainer', 'Bugs', 'Architecture',
    'Multi-Arch', 'Source', 'Version', 'Config-Version', 'Replaces',
    'Provides', 'Depends', 'Pre-Depends', 'Recommends', 'Suggests', 'Breaks',
    'Conflicts', 'Enhances', 'Conffiles', 'Description', 'Triggers-Pending',
    'Triggers-Awaited',
)


class ArchiveError(RuntimeError):
    """Archive metadata or a package cannot be used as pinned."""


# ── deb822 ──────────────────────────────────────────────────────────────
def parse_deb822(text: str) -> list[dict[str, str]]:
    """Stanzas as ordered dicts; continuation lines keep their newlines."""
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
            current, key = {}, None
            continue
        if line[0] in ' \t':
            if key is None:
                raise ArchiveError(f'continuation line without a field: {line!r}')
            current[key] += '\n' + line
            continue
        name, separator, value = line.partition(':')
        if not separator:
            raise ArchiveError(f'malformed deb822 line: {line!r}')
        key = name.strip()
        current[key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def format_deb822(stanza: dict[str, str]) -> str:
    lines = []
    for name, value in stanza.items():
        head, *rest = value.split('\n')
        lines.append(f'{name}: {head}'.rstrip() if head else f'{name}:')
        lines.extend(rest)
    return '\n'.join(lines) + '\n'


# ── versions ────────────────────────────────────────────────────────────
def _is_digit(character: str) -> bool:
    return '0' <= character <= '9'


def _order(character: str) -> int:
    """dpkg's order(): '~' sorts before everything, letters before symbols."""
    if _is_digit(character):
        return 0
    if ('a' <= character <= 'z') or ('A' <= character <= 'Z'):
        return ord(character)
    if character == '~':
        return -1
    return ord(character) + 256


def _verrevcmp(a: str, b: str) -> int:
    """dpkg's verrevcmp() (lib/dpkg/version.c), returning -1, 0 or 1."""
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not _is_digit(a[i])) or (j < len(b) and not _is_digit(b[j])):
            left = _order(a[i]) if i < len(a) else 0
            right = _order(b[j]) if j < len(b) else 0
            if left != right:
                return -1 if left < right else 1
            i += 1
            j += 1
        while i < len(a) and a[i] == '0':
            i += 1
        while j < len(b) and b[j] == '0':
            j += 1
        while i < len(a) and j < len(b) and _is_digit(a[i]) and _is_digit(b[j]):
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and _is_digit(a[i]):
            return 1
        if j < len(b) and _is_digit(b[j]):
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0


def _split_version(version: str) -> tuple[int, str, str]:
    epoch, colon, rest = version.partition(':')
    if not colon:
        epoch, rest = '0', version
    upstream, hyphen, revision = rest.rpartition('-')
    if not hyphen:
        upstream, revision = rest, ''
    return int(epoch or 0), upstream, revision


def compare_versions(left: str, right: str) -> int:
    """-1, 0 or 1, like ``dpkg --compare-versions``."""
    a, b = _split_version(left), _split_version(right)
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    return _verrevcmp(a[1], b[1]) or _verrevcmp(a[2], b[2])


def version_satisfies(version: str, operator: str, wanted: str) -> bool:
    result = compare_versions(version, wanted)
    return {
        '<<': result < 0, '<=': result <= 0, '=': result == 0,
        '>=': result >= 0, '>>': result > 0,
    }[operator]


_RELATION = re.compile(
    r'^(?P<name>[a-z0-9][a-z0-9+.\-]*)(?::(?P<arch>[a-z0-9\-]+))?'
    r'\s*(?:\((?P<op><<|<=|=|>=|>>)\s*(?P<version>[^)\s]+)\s*\))?'
    r'\s*(?:\[[^\]]*\])?\s*(?:<[^>]*>)?$')


@dataclass(frozen=True)
class Relation:
    name: str
    operator: str = ''
    version: str = ''


def parse_relations(value: str) -> list[list[Relation]]:
    """``a (>= 1) | b, c`` -> [[a>=1, b], [c]]."""
    groups: list[list[Relation]] = []
    for group in filter(None, (part.strip() for part in value.replace('\n', ' ').split(','))):
        alternatives = []
        for alternative in group.split('|'):
            match = _RELATION.match(alternative.strip())
            if match is None:
                raise ArchiveError(f'unparsable relation {alternative.strip()!r}')
            alternatives.append(Relation(match['name'], match['op'] or '', match['version'] or ''))
        groups.append(alternatives)
    return groups


# ── signed indexes ──────────────────────────────────────────────────────
def verify_inrelease(data: bytes, keyring: str) -> None:
    """gpgv-verify a clearsigned InRelease file against ``keyring``."""
    result = subprocess.run(
        ['gpgv', '--keyring', keyring, '-'], input=data, capture_output=True, check=False)
    if result.returncode != 0:
        raise ArchiveError('InRelease signature verification failed: '
                           + result.stderr.decode('utf-8', 'replace').strip())


def inrelease_sha256(data: bytes) -> dict[str, tuple[str, int]]:
    """Map index path -> (sha256, size) from the signed SHA256 section."""
    text = data.decode('utf-8')
    body = text.split('-----BEGIN PGP SIGNATURE-----', 1)[0]
    if '-----BEGIN PGP SIGNED MESSAGE-----' in body:
        body = body.split('\n\n', 1)[1]
    (release,) = parse_deb822(body)
    table: dict[str, tuple[str, int]] = {}
    for line in release.get('SHA256', '').split('\n'):
        fields = line.split()
        if len(fields) == 3:
            table[fields[2]] = (fields[0], int(fields[1]))
    return table


def load_packages_index(compressed: bytes) -> list[dict[str, str]]:
    return parse_deb822(lzma.decompress(compressed).decode('utf-8'))


# ── closure ─────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    stanza: dict[str, str]
    suite: str

    @property
    def name(self) -> str:
        return self.stanza['Package']

    @property
    def version(self) -> str:
        return self.stanza['Version']


@dataclass
class Installed:
    """Name -> version of installed packages, plus what they provide."""
    versions: dict[str, str] = field(default_factory=dict)
    provides: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def add(self, stanza: dict[str, str]) -> None:
        """Record ``stanza`` as installed, replacing an older version's provides."""
        name, version = stanza['Package'], stanza['Version']
        self.versions[name] = version
        for virtual, providers in self.provides.items():
            self.provides[virtual] = [row for row in providers if row[0] != name]
        for group in parse_relations(stanza.get('Provides', '')):
            for relation in group:
                self.provides.setdefault(relation.name, []).append((name, relation.version))

    def satisfies(self, relation: Relation) -> bool:
        version = self.versions.get(relation.name)
        if version is not None and (
                not relation.operator or version_satisfies(version, relation.operator, relation.version)):
            return True
        for _provider, provided in self.provides.get(relation.name, []):
            if not relation.operator:
                return True
            if provided and version_satisfies(provided, relation.operator, relation.version):
                return True
        return False


def installed_from_status(text: str) -> Installed:
    installed = Installed()
    for stanza in parse_deb822(text):
        if stanza.get('Status', '').endswith(' installed'):
            installed.add(stanza)
    return installed


def best_candidates(indexes: dict[str, list[dict[str, str]]], architecture: str) -> dict[str, Candidate]:
    """Highest version of each package across suites, as apt would pick."""
    best: dict[str, Candidate] = {}
    for suite in sorted(indexes):
        for stanza in indexes[suite]:
            if stanza.get('Architecture') not in (architecture, 'all'):
                continue
            current = best.get(stanza['Package'])
            if current is None or compare_versions(stanza['Version'], current.version) > 0:
                best[stanza['Package']] = Candidate(stanza, suite)
    return best


def resolve_closure(requested: list[str], installed: Installed,
                    candidates: dict[str, Candidate]) -> list[Candidate]:
    """Pre-Depends/Depends closure of ``requested`` that ``installed`` lacks.

    Like ``apt install``, a base package whose installed version no longer
    satisfies a relation is upgraded to the candidate; the result includes
    it, and ``install_deb`` replaces the old version's files.
    """
    chosen: dict[str, Candidate] = {}
    state = Installed(dict(installed.versions), {k: list(v) for k, v in installed.provides.items()})
    queue = [name for name in requested if name not in installed.versions]
    while queue:
        name = queue.pop(0)
        if name in chosen:
            continue
        candidate = candidates.get(name)
        if candidate is None:
            raise ArchiveError(f'package {name} is not in the pinned indexes')
        chosen[name] = candidate
        state.add(candidate.stanza)
        relations = parse_relations(candidate.stanza.get('Pre-Depends', ''))
        relations += parse_relations(candidate.stanza.get('Depends', ''))
        for group in relations:
            if any(state.satisfies(relation) for relation in group):
                continue
            pick = next((relation for relation in group if relation.name in candidates
                         and (not relation.operator or version_satisfies(
                             candidates[relation.name].version, relation.operator,
                             relation.version))), None)
            if pick is None:
                raise ArchiveError(f'{name}: no candidate satisfies {group}')
            queue.append(pick.name)
    return sorted(chosen.values(), key=lambda candidate: candidate.name)


# ── installation ────────────────────────────────────────────────────────
def _dpkg_deb(flag: str, deb: str) -> bytes:
    result = subprocess.run(['dpkg-deb', flag, deb], capture_output=True, check=False)
    if result.returncode != 0:
        raise ArchiveError(f'dpkg-deb {flag} {deb}: {result.stderr.decode("utf-8", "replace").strip()}')
    return result.stdout


@dataclass
class InstalledPackage:
    name: str
    version: str
    status_stanza: dict[str, str]
    info_files: dict[str, bytes]
    paths: list[str]


@dataclass(frozen=True)
class PathFilter:
    """dpkg's --path-exclude/--path-include rules (dpkg(1), src/main/filters.c).

    The last matching pattern wins; ``*`` also matches ``/``. An excluded
    directory or symlink survives when an include pattern's literal prefix
    and its path are prefixes of one another, so included files keep their
    parents. Excluded paths stay in the package's file list, as dpkg keeps
    them.
    """
    rules: tuple[tuple[bool, str], ...] = ()

    def skips(self, path: str, member: tarfile.TarInfo) -> bool:
        absolute = '/' + path
        skip = False
        for include, pattern in self.rules:
            if fnmatch.fnmatchcase(absolute, pattern):
                skip = not include
        if skip and (member.isdir() or member.issym()):
            for include, pattern in self.rules:
                if not include:
                    continue
                literal = re.split(r'[*?\[\\]', pattern, maxsplit=1)[0]
                if absolute.startswith(literal) or literal.startswith(absolute):
                    return False
        return skip


def dpkg_path_filter(tree: Tree) -> PathFilter:
    """Rules from /etc/dpkg/dpkg.cfg.d/*, read in name order as dpkg does."""
    prefix = 'etc/dpkg/dpkg.cfg.d/'
    rules: list[tuple[bool, str]] = []
    for path in sorted(path for path in tree.entries if path.startswith(prefix)):
        entry = tree.get(path)
        if entry.type != 'file':
            continue
        with open(entry.source, encoding='utf-8') as handle:
            for line in handle:
                option, _, value = line.strip().partition('=')
                if option in ('path-exclude', 'path-include') and value:
                    rules.append((option == 'path-include', value.strip()))
    return PathFilter(tuple(rules))


def install_deb(tree: Tree, deb: str, architecture: str,
                path_filter: PathFilter | None = None) -> InstalledPackage:
    """Unpack one .deb into ``tree`` and return its dpkg database records.

    Paths go through the tree's directory symlinks (merged /usr) exactly as
    dpkg unpacks them, and ``path_filter`` drops what the base's dpkg
    configuration excludes; the ``.list`` file records every path as the
    package ships it. No maintainer script runs.
    """
    control_members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(_dpkg_deb('--ctrl-tarfile', deb))) as control_tar:
        for member in control_tar:
            if member.isfile():
                name = member.name[2:] if member.name.startswith('./') else member.name
                control_members[name] = control_tar.extractfile(member).read()
    (control,) = parse_deb822(control_members.pop('control').decode('utf-8'))
    with tarfile.open(fileobj=io.BytesIO(_dpkg_deb('--fsys-tarfile', deb))) as data_tar:
        try:
            shipped = tree.import_tar(data_tar, follow_parent_links=True,
                                      skip=path_filter.skips if path_filter else None)
        except TreeError as error:
            raise ArchiveError(f'{control["Package"]}: {error}') from error

    package = control['Package']
    stem = f'{package}:{architecture}' if control.get('Multi-Arch') == 'same' else package
    info_files: dict[str, bytes] = {}
    listing = ['/.'] + [f'/{path}' for path in shipped]
    info_files[f'{stem}.list'] = ('\n'.join(listing) + '\n').encode('utf-8')
    for member, content in sorted(control_members.items()):
        info_files[f'{stem}.{member}'] = content

    stanza = dict(control)
    stanza['Status'] = 'install ok installed'
    conffiles = control_members.get('conffiles', b'').decode('utf-8').split()
    if conffiles:
        rows = []
        for conffile in conffiles:
            entry = tree.get(tree.resolve_parent(conffile.lstrip('/')))
            if entry is None or entry.type != 'file':
                raise ArchiveError(f'{package}: conffile {conffile} is not a shipped file')
            with open(entry.source, 'rb') as handle:
                rows.append(f' {conffile} {hashlib.md5(handle.read()).hexdigest()}')
        stanza['Conffiles'] = '\n' + '\n'.join(rows)
    return InstalledPackage(package, control['Version'], stanza, info_files, shipped)


def info_stem(tree: Tree, package: str, architecture: str) -> str | None:
    """The info-file stem dpkg uses for an installed package, if registered."""
    for stem in (package, f'{package}:{architecture}'):
        if tree.get(f'{DPKG_INFO}/{stem}.list') is not None:
            return stem
    return None


def remove_installed(tree: Tree, package: str, architecture: str) -> None:
    """Delete an installed package's non-directory paths and info files.

    The upgrade half of ``dpkg --unpack``: the new version is unpacked
    afterwards. Directories stay, as dpkg keeps directories other packages
    may share.
    """
    stem = info_stem(tree, package, architecture)
    if stem is None:
        raise ArchiveError(f'{package} has no dpkg file list to upgrade from')
    with open(tree.get(f'{DPKG_INFO}/{stem}.list').source, encoding='utf-8') as handle:
        listed = [line.strip() for line in handle if line.strip() not in ('', '/.')]
    for path in sorted(listed, reverse=True):
        resolved = tree.resolve_parent(path.lstrip('/'))
        entry = tree.get(resolved)
        if entry is not None and entry.type != 'dir':
            tree.remove(resolved)
    prefix = f'{DPKG_INFO}/{stem}.'
    for path in [path for path in tree.entries if path.startswith(prefix)]:
        tree.remove(path)


def diverted_paths(tree: Tree) -> set[str]:
    """Paths dpkg diverts (``/var/lib/dpkg/diversions`` holds triples)."""
    entry = tree.get('var/lib/dpkg/diversions')
    if entry is None:
        return set()
    with open(entry.source, encoding='utf-8') as handle:
        lines = handle.read().splitlines()
    return {lines[index] for index in range(0, len(lines) - 2, 3)}


def unpack_packages(tree: Tree, debs: list[tuple[str, str]], architecture: str) -> list[InstalledPackage]:
    """Install pinned .debs; packages already registered are upgraded.

    Old versions are removed before anything is unpacked, so a file that
    moved between packages in the same transaction is never deleted.
    """
    upgraded = {name for name, _deb in debs if info_stem(tree, name, architecture)}
    for name in sorted(upgraded):
        remove_installed(tree, name, architecture)
    diverted = diverted_paths(tree)
    path_filter = dpkg_path_filter(tree)
    installed = []
    for name, deb in debs:
        package = install_deb(tree, deb, architecture, path_filter)
        if package.name != name:
            raise ArchiveError(f'{deb} contains {package.name}, pinned as {name}')
        clash = sorted(f'/{path}' for path in package.paths if f'/{path}' in diverted)
        if clash:
            raise ArchiveError(f'{name} ships diverted paths: {", ".join(clash)}')
        installed.append(package)
    register(tree, installed, upgraded)
    register_trigger_interests(tree, installed)
    return installed


def register_trigger_interests(tree: Tree, packages: list[InstalledPackage]) -> None:
    """Record ``interest`` lines the way dpkg's trigger database stores them."""
    for package in packages:
        stem = next((name[:-len('.triggers')] for name in package.info_files
                     if name.endswith('.triggers')), None)
        if stem is None:
            continue
        text = package.info_files[f'{stem}.triggers'].decode('utf-8')
        for line in text.splitlines():
            words = line.split('#', 1)[0].split()
            if len(words) != 2 or not words[0].startswith('interest'):
                continue
            directive, name = words
            suffix = '/noawait' if directive == 'interest-noawait' else ''
            if name.startswith('/'):
                path, row = 'var/lib/dpkg/triggers/File', f'{name} {package.name}{suffix}'
            else:
                path, row = f'var/lib/dpkg/triggers/{name}', f'{package.name}{suffix}'
            existing = tree.get(path)
            rows = []
            if existing is not None:
                with open(existing.source, encoding='utf-8') as handle:
                    rows = handle.read().splitlines()
            if row not in rows:
                rows.append(row)
            tree.add_bytes(path, ('\n'.join(rows) + '\n').encode('utf-8'), 0o644, replace=True)


def order_status_fields(stanza: dict[str, str]) -> dict[str, str]:
    ordered = {name: stanza[name] for name in STATUS_FIELD_ORDER if name in stanza}
    ordered.update((name, value) for name, value in stanza.items() if name not in ordered)
    return ordered


def register(tree: Tree, packages: list[InstalledPackage], upgraded: set[str]) -> None:
    """Add packages to the status file (sorted by name) and the info directory."""
    status = tree.get(DPKG_STATUS)
    if status is None or status.type != 'file':
        raise ArchiveError(f'the base has no {DPKG_STATUS}')
    with open(status.source, encoding='utf-8') as handle:
        stanzas = parse_deb822(handle.read())
    names = {package.name for package in packages}
    clash = sorted((names - upgraded) & {stanza['Package'] for stanza in stanzas})
    if clash:
        raise ArchiveError(f'already registered in the base: {", ".join(clash)}')
    stanzas = [stanza for stanza in stanzas if stanza['Package'] not in upgraded]
    stanzas += [package.status_stanza for package in packages]
    stanzas.sort(key=lambda stanza: (stanza['Package'], stanza.get('Architecture', '')))
    text = '\n'.join(format_deb822(order_status_fields(stanza)) for stanza in stanzas)
    tree.add_bytes(DPKG_STATUS, text.encode('utf-8'), status.mode, replace=True)
    for package in packages:
        for name, content in package.info_files.items():
            mode = 0o755 if name.rsplit('.', 1)[-1] in (
                'preinst', 'postinst', 'prerm', 'postrm', 'config') else 0o644
            tree.add_bytes(f'{DPKG_INFO}/{name}', content, mode)
