"""Disposable real tool executor parked after replacement, for SIGKILL tests.

The parent owns storage and this process. RPC credentials arrive on stdin only;
the ready file carries producing identities, never credentials or fake receipts.
"""
import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace


def main():
    import pytest
    from lib.storage.client import StorageClient
    from lib.storage.v2_client import StorageV2Client
    values=json.load(sys.stdin)
    endpoint=values['endpoint']
    client=(StorageV2Client(*endpoint,values['token'].encode(),owner_id=values['owner_id'],
                           tenant_id=values['tenant_id']) if values['native']
            else StorageClient(*endpoint,values['token']))
    from lib import turn_lifecycle, file_history
    from lib.project_mod import modifications
    from lib.project_mod.write_tools import _ops
    from tests.turn_lifecycle.test_task_event_delivery import _task
    from tests.turn_lifecycle.test_file_effect_inspection import write
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('lib.storage.get_storage_client',lambda write=False:client)
        patch.setenv('TOFU_TURN_DELTA_RECORD_MS','0')
        patch.setattr(modifications,'SESSIONS_DIR',values['journal'])
        patch.setattr(file_history,'track_edit',lambda *a,**k:None)
        patch.setattr(_ops,'_touch_for_vscode',lambda *a,**k:None)
        _,task=_task(turn_lifecycle,SimpleNamespace(client=client),'killed-file-writer')
        if values.get('child'):
            from tests.turn_lifecycle.test_child_file_effects import child_of
            task=child_of(task,Path(values['project']))
        def after_replace(*a,**k):
            ready=Path(values['ready']);temporary=ready.with_suffix('.pending')
            receipt={key:task[key] for key in ('id','convId','_turnId','_attemptId')}
            receipt['operationId']=task['toolRounds'][0]['executionEffect']['operationId']
            temporary.write_text(json.dumps(receipt)+'\n');os.replace(temporary,ready)
            while True:signal.pause()
        patch.setattr(_ops,'_record_modification_checked',after_replace)
        if values.get('child'):
            from tests.turn_lifecycle.test_child_file_effects import write as write_child
            write_child(task,Path(values['project']))
        else:
            write(task,Path(values['project']),append=True)
        raise AssertionError('crash probe unexpectedly returned')


if __name__=='__main__':main()
