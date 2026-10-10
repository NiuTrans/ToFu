"""Actual private Node bootstrap, secret isolation and provider process bounds."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from lib.identity import PrincipalContext
from lib.automations.errors import AutomationError
from lib.skills.automation.mtskills_cli import (
    MTSkillsCLI, MTSkillsCLIError, OwnedMTSkillsCredential, source_policy,
)

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    os.name != 'posix' or not shutil.which('node'), reason='POSIX Node metadata port required')]


@pytest.fixture
def metadata_port(tmp_path):
    # This controlled CLI fixture performs no provider, auth or model I/O.
    cli = tmp_path / 'fake-cli.mjs'
    cli.write_text(r"""
import { readFileSync, writeFileSync, closeSync } from 'node:fs';
import { spawn } from 'node:child_process';
const args = process.argv.slice(2);
const token = args[args.indexOf('--token') + 1];
const subject = args[0] === 'search' ? args[1] : args[2];
if (subject === 'fail') { process.stderr.write(token); process.exit(23); }
if (subject === 'duplicate') { console.log('{"id":"one","id":"two"}'); }
else if (subject === 'echo') { console.log(JSON.stringify({ value: token })); }
else if (subject === 'encoded-echo') {
  console.log('{"value":"' + [...token].map(c => '\\u' + c.charCodeAt(0).toString(16).padStart(4,'0')).join('') + '"}');
}
else if (subject === 'flood') { process.stdout.write('x'.repeat(1024 * 1024)); setInterval(()=>{}, 100); }
else if (subject === 'closed-pipes') {
  closeSync(1); closeSync(2); setInterval(()=>{}, 100);
}
else if (subject === 'hang') { setInterval(()=>{}, 100); }
else if (subject === 'descendant') {
  const child = spawn(process.execPath, ['-e', 'setInterval(()=>{},100)'], { stdio: 'inherit' });
  writeFileSync(new URL('./child.pid', import.meta.url), String(child.pid));
  process.exit(0);
}
else if (args[0] === 'search') {
  const items = subject === 'ambiguous' ? [{id:'one',name:subject},{id:'two',name:subject}]
    : subject === 'missing' ? [{id:'other',name:'other'}] : [{id:'source-123',name:subject}];
  console.log(JSON.stringify({ items }));
} else {
  console.log(JSON.stringify({ id: subject, observation: {
    osArgv: readFileSync('/proc/self/cmdline','utf8').split('\0'),
    home: process.env.HOME, temporary: process.env.TMPDIR,
    tokenCache: process.env.MTSKILLS_SSO_TOKEN_DIR,
    environmentKeys: Object.keys(process.env), debug: process.env.MTSKILLS_DEBUG,
    effectiveArgs: args.filter((value,index)=>index!==args.indexOf('--token')+1)
  }}));
}
""")
    policy = source_policy()
    policy['metadata'].update(timeoutSeconds=2, pollSeconds=0.01)
    port = MTSkillsCLI(cli_path=cli, node_path=shutil.which('node'), policy=policy)
    principal = PrincipalContext.user(subject_id='owned-source', owner_user_id=7,
        tenant_id='source-tenant', scopes={'skills:read'})
    credential = OwnedMTSkillsCredential(7, 'source-tenant', 'prod', 'fixture-owner-token-2026')
    return port, principal, credential, tmp_path


def test_three_requested_sources_use_exact_cli_metadata(metadata_port):
    port, principal, credential, _ = metadata_port
    for name in ('citadel', 'hope-skills', 'llm-skills'):
        assert port.search(name, principal=principal, credential=credential) == {
            'name': name, 'sourceId': 'source-123'}


def test_private_descriptor_and_home_do_not_inherit_host_secrets(metadata_port, monkeypatch):
    port, principal, credential, _ = metadata_port
    monkeypatch.setenv('CIBA_MIS', 'unrelated-host-identity')
    monkeypatch.setenv('UNRELATED_API_KEY', 'must-never-enter-the-provider')
    monkeypatch.setenv('MTSKILLS_DEBUG', '1')
    monkeypatch.setenv('MTSKILLS_SSO_TOKEN_DIR', '/host/private/cache')
    value = port.detail('source-123', principal=principal, credential=credential)
    observation = value['observation']
    assert credential.token not in '\n'.join(observation['osArgv'])
    assert 'CIBA_MIS' not in observation['environmentKeys']
    assert 'UNRELATED_API_KEY' not in observation['environmentKeys']
    assert observation['debug'] == '0'
    assert observation['tokenCache'] != '/host/private/cache'
    home = Path(observation['home'])
    assert Path(observation['temporary']).parent == home
    assert Path(observation['tokenCache']).parent == home
    assert not home.exists()  # Child is reaped before the private home disappears.
    assert observation['effectiveArgs'] == ['fetchdata','detail','source-123','--env','prod','--token']
    assert credential.token not in repr(credential)


@pytest.mark.parametrize('owner,tenant', [(8,'source-tenant'), (7,'foreign-tenant')])
def test_foreign_credentials_fail_before_process_start(metadata_port, monkeypatch, owner, tenant):
    port, principal, credential, _ = metadata_port
    foreign = OwnedMTSkillsCredential(owner, tenant, 'prod', credential.token)
    def forbidden(*args, **kwargs):
        raise AssertionError('Foreign principal reached process launch')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(PermissionError):
        port.detail('source-123', principal=principal, credential=foreign)


@pytest.mark.parametrize('name', ['missing', 'ambiguous'])
def test_search_never_falls_back_to_another_package(metadata_port, name):
    port, principal, credential, _ = metadata_port
    with pytest.raises(AutomationError, match='unavailable or ambiguous'):
        port.search(name, principal=principal, credential=credential)


@pytest.mark.parametrize('subject', ['duplicate', 'echo', 'encoded-echo'])
def test_untrusted_json_and_echoed_secrets_are_rejected(metadata_port, subject):
    port, principal, credential, _ = metadata_port
    with pytest.raises(MTSkillsCLIError) as failure:
        port.detail(subject, principal=principal, credential=credential)
    assert failure.value.code == 'skills_provider_response'
    assert credential.token not in str(failure.value)


def test_failed_cli_reports_exit_without_remote_secret_stderr(metadata_port):
    port, principal, credential, _ = metadata_port
    with pytest.raises(MTSkillsCLIError) as failure:
        port.detail('fail', principal=principal, credential=credential)
    assert failure.value.exit_code == 23
    assert not failure.value.timed_out and not failure.value.cancelled
    assert credential.token not in str(failure.value)


@pytest.mark.parametrize('subject', ['hang', 'closed-pipes', 'flood', 'descendant'])
def test_all_interrupted_paths_reap_and_close_actual_process(metadata_port, monkeypatch, subject):
    port, principal, credential, directory = metadata_port
    port._policy['metadata']['timeoutSeconds'] = 1
    process_port = subprocess.Popen
    observed = []
    def capture(*args, **kwargs):
        child = process_port(*args, **kwargs)
        observed.append((child, Path(kwargs['cwd'])))
        return child
    monkeypatch.setattr(subprocess, 'Popen', capture)
    with pytest.raises(MTSkillsCLIError) as failure:
        port.detail(subject, principal=principal, credential=credential)
    assert failure.value.output_limited if subject == 'flood' else failure.value.timed_out
    child, home = observed[0]
    assert child.returncode is not None
    assert child.stdout.closed and child.stderr.closed and not home.exists()
    if subject == 'descendant':
        pid = (directory / 'child.pid').read_text()
        status = Path('/proc') / pid / 'stat'
        # A container init may briefly retain a dead orphan as a zombie; it
        # must have exited and cannot execute against the removed home.
        assert not status.exists() or status.read_text().split(') ',1)[1].startswith('Z ')


def test_timeout_and_cancellation_are_both_reported(metadata_port):
    port, principal, credential, _ = metadata_port
    clock = iter([0, 10])
    port._clock = lambda: next(clock)
    with pytest.raises(MTSkillsCLIError) as failure:
        port.detail('hang', principal=principal, credential=credential, cancelled=lambda: True)
    assert failure.value.timed_out and failure.value.cancelled


@pytest.mark.parametrize('token', ['', 'token\nheader', 'token\x00', 'x'*8193])
def test_invalid_credentials_fail_closed(metadata_port, token):
    port, principal, _, _ = metadata_port
    with pytest.raises(AutomationError, match='token is unavailable'):
        port.detail('source-123', principal=principal,
            credential=OwnedMTSkillsCredential(7,'source-tenant','prod',token))
