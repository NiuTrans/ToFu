"""Complete edit batches retain ordering, failure isolation and undo authority."""
import pytest

from lib.project_mod.write_tools import _ops
from lib.project_mod import modifications as mods

pytestmark=pytest.mark.unit


@pytest.fixture
def project(tmp_path,monkeypatch):
    import lib.file_history as history
    monkeypatch.setattr(history,'is_enabled',lambda:False)
    monkeypatch.setattr(mods,'SESSIONS_DIR',str(tmp_path/'journals'))
    root=tmp_path/'project';root.mkdir()
    monkeypatch.setattr(_ops,'_should_record_modification',lambda *a,**kw:True)
    monkeypatch.setattr(_ops,'_mod_attribution',lambda target,base,rel,**kw:(base,rel))
    return root


def replacements(count,path='file.txt',unified=False):
    return [({'path':path,'operation':'replace','anchor':f'STATE_{n:03d}',
              'content':f'STATE_{n+1:03d}'} if unified else
             {'path':path,'search':f'STATE_{n:03d}','replace':f'STATE_{n+1:03d}'})
            for n in range(count)]


@pytest.mark.parametrize('entry',['edit_file','apply_diffs'])
def test_complete_sixty_four_edit_chain_has_one_verified_undo_transition(project,entry):
    target=project/'file.txt';original=b'STATE_000\n';target.write_bytes(original)
    edits=replacements(64,unified=entry=='edit_file')
    function=_ops.tool_edit_file if entry=='edit_file' else _ops.tool_apply_diffs
    result=function(str(project),edits,task_id='demand-task')
    assert result.startswith('Applied 64/64'),result
    assert '[64] OK' in result
    assert target.read_bytes()==b'STATE_064\n'
    rows=mods.get_modifications(str(project));assert len(rows)==1
    undone=mods.undo_task_modifications(str(project),'demand-task')
    assert undone['failed']==0 and undone['undone']==1
    assert target.read_bytes()==original


def test_atomic_preflight_includes_failure_after_old_thirtieth_edit(project):
    target=project/'file.txt';original=b'STATE_000\n';target.write_bytes(original)
    edits=replacements(40)+[{'path':'file.txt','search':'missing-late-anchor','replace':'never'}]
    result=_ops.tool_apply_diffs(str(project),edits,atomic=True,task_id='demand-task')
    assert '[41] FAIL' in result
    assert 'atomic' in result.lower()
    assert target.read_bytes()==original
    assert mods.get_modifications(str(project))==[]


def test_late_invalid_edit_does_not_skip_following_valid_operation(project):
    target=project/'file.txt';target.write_text('STATE_000\n')
    edits=replacements(40,unified=True)+[{'bad':'invalid'},
        {'path':'file.txt','operation':'replace','anchor':'STATE_040','content':'FINAL_VALID_SOURCE'}]
    result=_ops.tool_edit_file(str(project),edits,task_id='demand-task',atomic=False)
    assert result.startswith('Applied 41/42'),result
    assert '[41] FAIL' in result and '[42] OK' in result
    assert target.read_text()=='FINAL_VALID_SOURCE\n'
    undone=mods.undo_task_modifications(str(project),'demand-task')
    assert undone['failed']==0 and undone['undone']==1
    assert target.read_text()=='STATE_000\n'


def test_all_thirty_two_insertions_keep_order_and_remain_undoable(project):
    target=project/'file.txt';target.write_text('START\n')
    edits=[{'path':'file.txt','anchor':'START' if n==0 else f'ITEM_{n-1:03d}',
            'content':f'ITEM_{n:03d}','position':'after'} for n in range(32)]
    result=_ops.tool_insert_contents(str(project),edits,task_id='demand-task')
    assert '32/32' in result,result
    text=target.read_text()
    assert text.splitlines()==['START']+[f'ITEM_{n:03d}' for n in range(32)]
    undone=mods.undo_task_modifications(str(project),'demand-task')
    assert undone['failed']==0 and undone['undone']==32
    assert target.read_text()=='START\n'


def test_registered_schema_accepts_complete_edit_batch():
    from jsonschema import Draft202012Validator
    from lib.tools.project import PROJECT_TOOLS
    schema=next(tool['function']['parameters'] for tool in PROJECT_TOOLS
                if tool['function']['name']=='edit_file')
    arguments={'description':'Apply the full ordered edit chain',
               'edits':replacements(64,unified=True)}
    Draft202012Validator(schema).validate(arguments)


def test_late_physical_write_failure_isolated_and_all_other_files_undo(project,monkeypatch):
    original_write=_ops._atomic_write_text
    for number in range(40):
        (project/f'file-{number:02d}.txt').write_text('BEFORE\n')
    def fail_late(target,content,*args,**kwargs):
        if str(target).endswith('file-31.txt'):
            raise OSError('late physical write failure')
        return original_write(target,content,*args,**kwargs)
    monkeypatch.setattr(_ops,'_atomic_write_text',fail_late)
    edits=[{'path':f'file-{number:02d}.txt','operation':'replace',
            'anchor':'BEFORE','content':'AFTER'} for number in range(40)]
    result=_ops.tool_edit_file(str(project),edits,task_id='demand-task')
    assert result.startswith('Applied 39/40'),result
    assert '[32] FAIL' in result and '[40] OK' in result
    assert 'late physical write failure' in result
    for number in range(40):
        assert (project/f'file-{number:02d}.txt').read_text()==('BEFORE\n' if number==31 else 'AFTER\n')
    rows=mods.get_modifications(str(project));assert len(rows)==39
    assert not any(row.get('path')=='file-31.txt' for row in rows)
    undone=mods.undo_task_modifications(str(project),'demand-task')
    assert undone['failed']==0 and undone['undone']==39
    assert all(path.read_text()=='BEFORE\n' for path in project.glob('file-*.txt'))
