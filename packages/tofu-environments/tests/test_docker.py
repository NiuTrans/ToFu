from pathlib import Path
import json
import pytest
pytestmark = pytest.mark.unit
from tofu_environments import Actor, EnvironmentError
from tofu_environments.docker import DockerProvider

class Runner:
    timeout=10;executable='docker'
    def __init__(self):self.calls=[];self.inspect={};self.fail=False
    def run(self,args,**kw):
        self.calls.append((args,kw))
        if 'inspect' in args:return json.dumps([self.inspect])
        if args[0]=='create':return 'container-id'
        if self.fail:raise EnvironmentError('transport','unknown',uncertain=kw.get('mutation',False))
        return ''

@pytest.fixture
def spec():return {'profile':'project_development','project':'example','image':{'base':'ubuntu@sha256:'+'a'*64,'name':'example','instructions':'RUN true'},'workspace':{'name':'dev','cpu':2,'memory_gib':4,'ports':[8000]}}

def test_owned_context_docker_arguments_and_labels(tmp_path,spec):
    (tmp_path/'file').write_text('original');actor=Actor(3,project_root=str(tmp_path));runner=Runner();provider=DockerProvider(runner)
    evidence=provider.validate(actor,spec);image=provider.create_image(actor,spec,'request')
    provider.start_build(actor,spec,image,'request',admission=evidence)
    args,kwargs=runner.calls[-1]
    assert args[0]=='build' and kwargs['input_bytes'].startswith(b'FROM ubuntu@sha256:')
    assert 'tofu.environment.owner='+actor.scope in args
    runner.inspect={'Id':'sha256:'+'b'*64,'Config':{'Labels':{'tofu.environment.owner':actor.scope,'tofu.environment.operation':'request'}}}
    provider.create_workspace(actor,spec,image,'request')
    args,_=runner.calls[-1];assert '127.0.0.1::8000' in args and '4g' in args
    (tmp_path/'file').write_text('changed');assert provider.validate(actor,spec)!=evidence

def test_context_and_mount_escape_are_rejected(tmp_path,spec):
    actor=Actor(3,project_root=str(tmp_path));provider=DockerProvider(Runner())
    spec['image']['context']='..'
    with pytest.raises(EnvironmentError,match='inside'):provider.validate(actor,spec)
    spec['image']['context']='.';spec['workspace']['mounts']=[{'source':'/etc','target':'/root','read_only':False}]
    with pytest.raises(EnvironmentError,match='inside'):provider.validate(actor,spec)

def test_symlink_and_base_override_are_rejected(tmp_path,spec):
    actor=Actor(3,project_root=str(tmp_path));provider=DockerProvider(Runner())
    spec['image']['instructions']='FROM other'
    with pytest.raises(EnvironmentError,match='FROM'):provider.validate(actor,spec)
    spec['image']['instructions']='RUN true';(tmp_path/'escape').symlink_to('/etc/passwd')
    with pytest.raises(EnvironmentError,match='symlinks'):provider.validate(actor,spec)

def test_ready_requires_service_health_and_never_executes_model_probe(tmp_path,spec):
    actor=Actor(3,project_root=str(tmp_path));runner=Runner();provider=DockerProvider(runner)
    spec['profile']='tofu_service';spec['workspace']['service_port']=15000
    runner.inspect={'Config':{'Labels':{'tofu.environment.owner':actor.scope,'tofu.environment.operation':'operation'}},'State':{'Running':True,'Health':{'Status':'starting'}}}
    provider.readiness_probe=lambda _:False
    runner.inspect['NetworkSettings']={'Ports':{'15000/tcp':[{'HostIp':'127.0.0.1','HostPort':'25000'}]}}
    assert provider.workspace_status(actor,spec,'id','operation').state=='waiting'
    runner.inspect['State']['Health']['Status']='healthy'
    assert provider.workspace_status(actor,spec,'id','operation').state=='waiting'
    provider.readiness_probe=lambda _:True
    assert provider.workspace_status(actor,spec,'id','operation').state=='ready'
    assert not any(args[0]=='exec' for args,_ in runner.calls)

def test_foreign_resources_do_not_reconcile(tmp_path,spec):
    actor=Actor(3,project_root=str(tmp_path));runner=Runner();provider=DockerProvider(runner)
    runner.inspect={'Config':{'Labels':{'tofu.environment.owner':'other','tofu.environment.operation':'operation'}}}
    record={'phase':'uncertain','uncertain_phase':'creating_workspace','operation_id':'operation','plan':{'spec':spec}}
    assert provider.reconcile(actor,record) is None

def test_approval_context_is_copied_and_changed_context_never_builds(tmp_path,spec):
    actor=Actor(3,project_root=str(tmp_path));runner=Runner();provider=DockerProvider(runner)
    (tmp_path/'empty').mkdir();(tmp_path/'empty').chmod(0o750)
    source=tmp_path/'file';source.write_text('approved')
    evidence=provider.validate(actor,spec)
    source.write_text('changed')
    with pytest.raises(EnvironmentError,match='changed after approval'):
        provider.start_build(actor,spec,'image','operation',admission=evidence)
    assert not runner.calls
    source.write_text('approved')
    provider.start_build(actor,spec,'image','operation',admission=evidence)
    staging=Path(runner.calls[-1][0][-1]);assert staging!=tmp_path and not staging.exists()

def test_mutable_base_and_unknown_provider_options_are_rejected(tmp_path,spec):
    actor=Actor(3,project_root=str(tmp_path));provider=DockerProvider(Runner())
    spec['image']['base']='ubuntu:latest'
    with pytest.raises(EnvironmentError,match='immutable'):provider.validate(actor,spec)


def test_retagged_foreign_image_is_not_used_for_workspace(tmp_path,spec):
    runner=Runner();provider=DockerProvider(runner)
    runner.inspect={'Id':'foreign','Config':{'Labels':{}}}
    with pytest.raises(EnvironmentError,match='exact owned'):provider.create_workspace(Actor(3),spec,'tag','operation')
    assert all(args[0]!='create' for args,_ in runner.calls)
