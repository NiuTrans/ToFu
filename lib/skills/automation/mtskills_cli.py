"""Owned mtskills metadata and bounded binary HTTPS archive transport.

Entry points: owned credentials and search/detail/download_archive on MTSkillsCLI.
Dependencies: trusted Node bootstraps, source policy and PrincipalContext. This port never
installs packages, executes skills, borrows login state or imports a model loop.
Subprocesses own a disposable private home, bounded pipes and a process group;
every exit path kills descendants, reaps the child and closes all descriptors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote

from lib.identity import PrincipalContext
from lib.automations.errors import AutomationError


@lru_cache(maxsize=1)
def _policy_json():
    return (Path(__file__).resolve().parents[3] /
            'contracts/automation_skill_sources_v1.json').read_text()


def source_policy():
    """Return a fresh document; callers cannot mutate the shared policy."""
    return json.loads(_policy_json())


@dataclass(frozen=True, slots=True)
class OwnedMTSkillsCredential:
    owner_user_id: int
    tenant_id: str | None
    environment: str
    token: str = field(repr=False)

    def require(self, principal):
        if not isinstance(principal, PrincipalContext):
            raise TypeError('Skill source requires PrincipalContext')
        owner = principal.require_owner(context='automated skill source')
        principal.require_scope('skills:read')
        limits = source_policy()['metadata']
        if (type(self.owner_user_id) is not int or self.owner_user_id != owner
                or self.tenant_id != principal.tenant_id):
            raise PermissionError('Skill source credential ownership differs')
        if self.environment not in source_policy()['environments']:
            raise AutomationError('skills_provider_configuration', 'Unknown skill source environment')
        if (not isinstance(self.token, str) or not self.token.strip()
                or len(self.token) > limits['maximumCredentialCharacters']
                or any(ord(char) < 33 or ord(char) > 126 for char in self.token)):
            raise AutomationError('skills_provider_configuration', 'Skill source token is unavailable')


class MTSkillsCLIError(AutomationError):
    """Orthogonal subprocess observations without remote stderr or secrets."""
    def __init__(self, code, message, *, timed_out=False, cancelled=False,
                 output_limited=False, exit_code=None):
        super().__init__(code, message)
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.output_limited = output_limited
        self.exit_code = exit_code


def _closed_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate provider JSON key')
        result[key] = value
    return result


def _nonfinite_json(_):
    raise ValueError('Non-finite provider JSON')


def _reject_echoed_secret(value, token):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and token in item:
            raise MTSkillsCLIError('skills_provider_response', 'Skill source response contains private credential data')
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _process_group_live_members(group_id):
    """Observe live members after SIGKILL, including reparented descendants.

    A zombie has exited and cannot touch files. A D-state process with pending
    SIGKILL has not exited yet: retain the private home until it actually does.
    Each launched provider has a fresh session/group, so this cannot select an
    unrelated host process. No PID/command line is included in user output.
    """
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            text = (entry / 'stat').read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue  # The observed process exited during this sample.
        except PermissionError:
            continue  # hidepid may conceal other owners; this group retains our UID.
        fields = text[text.rfind(')') + 2:].split()
        if (len(fields) >= 4 and fields[2] == str(group_id)
                and fields[3] == str(group_id) and fields[0] not in {'Z','X'}):
            members.append(entry / 'stat')
    return members


def _await_process_group_exit(group_id, poll_seconds):
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return  # No member remains, including any zombies.
    while _process_group_live_members(group_id):
        # Never abandon an uninterruptible member while its private filesystem
        # is still reachable. Disposal owns this wait, after execution timed out.
        time.sleep(poll_seconds)


class MTSkillsCLI:
    def __init__(self, *, cli_path, node_path, clock=time.monotonic, policy=None,
                 tls_ca_path=None):
        # These are deployment-selected executable paths, never HTTP/step args.
        self.cli_path = Path(cli_path).resolve(strict=True)
        self.node_path = Path(node_path).resolve(strict=True)
        if not self.cli_path.is_file() or not self.node_path.is_file():
            raise ValueError('Installed CLI and Node must be regular files')
        self._clock = clock
        self._policy = source_policy() if policy is None else json.loads(json.dumps(policy))
        self._tls_ca = None if tls_ca_path is None else Path(tls_ca_path).resolve(strict=True)
        if self._tls_ca is not None and not self._tls_ca.is_file():
            raise ValueError('Explicit provider CA must be a regular file')

    def search(self, name, *, principal, credential, cancelled=lambda: False):
        limits = self._policy['metadata']
        if not isinstance(name, str) or re.fullmatch(limits['namePattern'], name) is None:
            raise AutomationError('invalid_skill_source', 'Skill source name is invalid')
        value = self._metadata(['search', name, '--json', '--page', str(limits['searchPage']),
            '--page-size', str(limits['searchPageSize'])], principal, credential, cancelled)
        items = value.get('items')
        if not isinstance(items, list) or len(items) > limits['searchPageSize']:
            raise AutomationError('skills_provider_response', 'Skill source page violates its bound')
        matches = [row for row in items if isinstance(row, dict) and row.get('name') == name]
        if len(matches) != 1:
            raise AutomationError('skills_provider_identity', 'Exact skill source is unavailable or ambiguous')
        identity = matches[0].get('id')
        self._source_id(identity)
        return {'name': name, 'sourceId': identity}

    def detail(self, source_id, *, principal, credential, cancelled=lambda: False):
        self._source_id(source_id)
        return self._metadata(['fetchdata', 'detail', source_id], principal, credential, cancelled)

    def download_archive(self, source_id, *, principal, credential, cancelled=lambda: False):
        """Read approved binary bytes, bypassing CLI pre-validation extraction."""
        self._source_id(source_id)
        self._require_credential(principal, credential)
        policy = self._policy
        archive = policy['archives']
        environment = policy['environments'][credential.environment]
        origin = environment['origin']
        if not origin.startswith('https://'):
            raise AutomationError('skills_provider_configuration', 'Skill source requires an HTTPS origin')
        path = archive['byIdPath'].format(sourceId=quote(source_id, safe=''))
        payload = {'url': origin + path + '?channel=cli', 'token': credential.token,
            'ssoEnvironment': environment['ssoEnvironment'], 'maximumBytes': archive['maximumBytes'],
            'timeoutMilliseconds': int(archive['timeoutSeconds'] * 1000), 'exitCodes': archive['exitCodes']}
        limits = dict(policy['metadata'], maximumOutputBytes=archive['maximumBytes'],
            timeoutSeconds=archive['timeoutSeconds'])
        try:
            return self._invoke(Path(__file__).with_name('mtskills_archive.mjs'),
                payload, cancelled, limits, binary=True)
        except MTSkillsCLIError as exc:
            observations = dict(timed_out=exc.timed_out, cancelled=exc.cancelled,
                output_limited=exc.output_limited, exit_code=exc.exit_code)
            if exc.exit_code == archive['exitCodes']['tooLarge']:
                observations['output_limited'] = True
                raise MTSkillsCLIError('skills_archive_too_large', 'Skill source archive exceeds its byte bound',
                    **observations) from None
            if exc.exit_code == archive['exitCodes']['authenticationRequired']:
                raise MTSkillsCLIError('skills_provider_authentication', 'Owned skill source authentication is required',
                    **observations) from None
            if exc.exit_code == archive['exitCodes']['timedOut']:
                observations['timed_out'] = True
                raise MTSkillsCLIError('skills_provider_interrupted', 'Skill source archive deadline exceeded',
                    **observations) from None
            raise

    def _source_id(self, identity):
        if (not isinstance(identity, str)
                or re.fullmatch(self._policy['metadata']['sourceIdPattern'], identity) is None):
            raise AutomationError('skills_provider_identity', 'Skill source identifier is invalid')

    def _require_credential(self, principal, credential):
        if not isinstance(credential, OwnedMTSkillsCredential):
            raise TypeError('Skill source requires an owned credential')
        credential.require(principal)
        if os.name != 'posix' or not Path('/proc/self/stat').is_file():
            raise AutomationError('skills_provider_unavailable',
                'Installed CLI metadata requires observable Linux process-group containment')

    def _metadata(self, args, principal, credential, cancelled):
        self._require_credential(principal, credential)
        limits = self._policy['metadata']
        bootstrap = Path(__file__).with_name('mtskills_metadata.mjs')
        payload = {'cliPath': str(self.cli_path),
            'args': [*args, '--env', credential.environment], 'token': credential.token}
        value = self._invoke(bootstrap, payload, cancelled, limits, binary=False)
        _reject_echoed_secret(value, credential.token)
        return value

    def _invoke(self, bootstrap, input_value, cancelled, limits, *, binary):
        payload = json.dumps(input_value).encode()
        with tempfile.TemporaryDirectory(prefix='tofu-mtskills-metadata-') as temporary:
            home = Path(temporary)
            (home / 'tmp').mkdir(mode=0o700)
            (home / 'tokens').mkdir(mode=0o700)
            # An allowlist prevents host credentials, CIBA_MIS and debug flags
            # from entering the provider; the default cache is also private.
            environment = {'PATH': str(self.node_path.parent) + os.pathsep + os.defpath,
                'HOME': str(home), 'TMPDIR': str(home / 'tmp'),
                'MTSKILLS_SSO_TOKEN_DIR': str(home / 'tokens'), 'MTSKILLS_DEBUG': '0'}
            if self._tls_ca is not None:
                environment['NODE_EXTRA_CA_CERTS'] = str(self._tls_ca)
            with tempfile.TemporaryFile(dir=home) as input_file:
                input_file.write(payload)
                input_file.seek(0)
                try:
                    process = subprocess.Popen([str(self.node_path), str(bootstrap)],
                        stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=home, env=environment, start_new_session=True)
                except OSError:
                    raise MTSkillsCLIError('skills_provider_unavailable', 'Skill source subprocess cannot start') from None
                return self._collect(process, cancelled, limits, binary=binary)

    def _collect(self, process, cancelled, limits, *, binary):
        stdout = bytearray()
        total = 0
        timed_out = was_cancelled = limited = False
        exit_code = None
        started = self._clock()
        try:
            with selectors.DefaultSelector() as selector:
                for name, pipe in [('stdout', process.stdout), ('stderr', process.stderr)]:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map():
                    timed_out = self._clock() - started >= limits['timeoutSeconds']
                    was_cancelled = bool(cancelled())
                    if timed_out or was_cancelled:
                        break
                    for key, _ in selector.select(limits['pollSeconds']):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > limits['maximumOutputBytes']:
                            limited = True
                            break
                        if key.data == 'stdout':
                            stdout.extend(chunk)
                    if limited:
                        break
                # Closing the pipes is not proof of process exit: a CLI could
                # close stdout/stderr and then keep running indefinitely.
                while not (timed_out or was_cancelled or limited):
                    exit_code = process.poll()
                    if exit_code is not None:
                        break
                    timed_out = self._clock() - started >= limits['timeoutSeconds']
                    was_cancelled = bool(cancelled())
                    if not timed_out and not was_cancelled:
                        time.sleep(limits['pollSeconds'])
                exit_code = process.poll()
        finally:
            # Kill the group even if the leader exited: descendants may still
            # hold pipes or continue work. Then reap before removing the home.
            try:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # This process group has already fully exited.
            finally:
                try:
                    process.wait()
                    _await_process_group_exit(process.pid, limits['pollSeconds'])
                finally:
                    process.stdout.close()
                    process.stderr.close()
        exit_code = process.returncode
        observations = dict(timed_out=timed_out, cancelled=was_cancelled,
            output_limited=limited, exit_code=exit_code)
        if timed_out or was_cancelled or limited:
            raise MTSkillsCLIError('skills_provider_interrupted',
                'Skill source command did not complete within its execution bounds', **observations)
        if exit_code != 0:
            raise MTSkillsCLIError('skills_provider_failed', 'Skill source command failed', **observations)
        if binary:
            return bytes(stdout)
        try:
            value = json.loads(stdout, object_pairs_hook=_closed_json,
                parse_constant=_nonfinite_json)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise MTSkillsCLIError('skills_provider_response', 'Skill source metadata is invalid JSON', **observations) from None
        if not isinstance(value, dict):
            raise MTSkillsCLIError('skills_provider_response', 'Skill source metadata must be an object', **observations)
        return value
