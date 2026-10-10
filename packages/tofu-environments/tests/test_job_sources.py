"""Source ownership, immutable input identity and hostile ZIP expansion boundaries."""
import hashlib
import stat
import zipfile
import pytest
from tofu_environments.contracts import Actor,EnvironmentError
from tofu_environments.job_sources import extract_job_source

def archive(tmp_path,entries):
    root=tmp_path/'project';root.mkdir(exist_ok=True);path=root/'source.zip'
    with zipfile.ZipFile(path,'w') as z:
        for name,data in entries:z.writestr(name,data)
    stage=tmp_path/'stage';stage.mkdir()
    return Actor(7,project_root=str(root)),{'uri':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},stage

def test_real_archive_is_pinned_and_reproducibly_extracted(tmp_path):
    actor,source,stage=archive(tmp_path,[('src/main.py',b'print(42)'),('data.json',b'{}')]);r=extract_job_source(actor,source,stage,byte_limit=4096,file_limit=10)
    assert r['source_sha256']==source['sha256'];assert r['total_bytes']==11
    assert (stage/'src/main.py').read_bytes()==b'print(42)'
    assert r['files'][0]['sha256']==hashlib.sha256(b'print(42)').hexdigest()

@pytest.mark.parametrize('name',['../escape','/absolute','folder/../../escape','folder\\escape','C:/escape'])
def test_archive_cannot_write_outside_staging(tmp_path,name):
    a,s,p=archive(tmp_path,[(name,b'bad')])
    with pytest.raises(EnvironmentError,match='unsafe path'):extract_job_source(a,s,p,byte_limit=4096,file_limit=10)
    assert not list(p.iterdir())

def test_source_symlink_outside_actor_project_is_rejected(tmp_path):
    a,s,p=archive(tmp_path,[('ok',b'ok')]);original=tmp_path/'outside.zip';original.write_bytes(__import__('pathlib').Path(s['uri']).read_bytes());link=__import__('pathlib').Path(a.project_root)/'link.zip';link.symlink_to(original);s['uri']=str(link)
    with pytest.raises(EnvironmentError) as error:extract_job_source(a,s,p,byte_limit=4096,file_limit=10)
    assert error.value.code=='source_outside_project'

def test_wrong_identity_writes_no_source_files(tmp_path):
    a,s,p=archive(tmp_path,[('ok',b'ok')]);s['sha256']='0'*64
    with pytest.raises(EnvironmentError) as error:extract_job_source(a,s,p,byte_limit=4096,file_limit=10)
    assert error.value.code=='source_changed';assert not list(p.iterdir())

def test_expansion_and_reserved_files_fail_before_extract(tmp_path):
    a,s,p=archive(tmp_path,[('runner.py',b'x'*2048)])
    with pytest.raises(EnvironmentError):extract_job_source(a,s,p,byte_limit=4096,file_limit=10,reserved=['runner.py'])
    assert not list(p.iterdir())
    with pytest.raises(EnvironmentError):extract_job_source(a,s,p,byte_limit=128,file_limit=10)

def test_archive_symlink_is_rejected(tmp_path):
    a,s,p=archive(tmp_path,[]);path=__import__('pathlib').Path(s['uri']);info=zipfile.ZipInfo('link');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
    with zipfile.ZipFile(path,'w') as z:z.writestr(info,'/outside')
    s['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(EnvironmentError) as error:extract_job_source(a,s,p,byte_limit=4096,file_limit=10)
    assert error.value.code=='source_type_invalid'

def test_compressed_zip_bomb_is_rejected_before_expansion(tmp_path):
    a,s,p=archive(tmp_path,[]);path=__import__('pathlib').Path(s['uri'])
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('large',b'x'*100000)
    assert path.stat().st_size<4096;s['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(EnvironmentError) as error:extract_job_source(a,s,p,byte_limit=4096,file_limit=10)
    assert error.value.code=='source_limit';assert not list(p.iterdir())

def test_source_mutation_after_hash_cannot_change_extracted_bytes(tmp_path,monkeypatch):
    from pathlib import Path
    import tofu_environments.job_sources as sources
    a,s,p=archive(tmp_path,[('value',b'original')]);replacement=tmp_path/'replacement.zip'
    with zipfile.ZipFile(replacement,'w') as z:z.writestr('value',b'mutated!')
    original_zip=zipfile.ZipFile
    def mutate_after_copy(stream,*args,**kwargs):
        Path(s['uri']).write_bytes(replacement.read_bytes())
        return original_zip(stream,*args,**kwargs)
    monkeypatch.setattr(sources.zipfile,'ZipFile',mutate_after_copy)
    receipt=extract_job_source(a,s,p,byte_limit=4096,file_limit=10)
    assert (p/'value').read_bytes()==b'original'
    assert receipt['files'][0]['sha256']==hashlib.sha256(b'original').hexdigest()
