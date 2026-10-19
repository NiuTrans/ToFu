"""Final state snapshots preserve history and cannot acquire terminal authority."""
import copy
from unittest.mock import Mock,patch
import pytest
from lib.tasks_pkg.orchestrator._completion_projection import publish_final_messages_snapshot

pytestmark=pytest.mark.unit


def test_snapshot_is_state_with_tools_and_sanitized_media_without_history_mutation():
    messages=[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,QUJD'}}]},
              {'role':'assistant','content':'answer'}]
    before=copy.deepcopy(messages)
    task={'id':'task','_userId':7,'status':'running','messages':messages,'_contextManifest':[{'source':'fixture'}]}
    tools=[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]
    publish=Mock()
    publish_final_messages_snapshot(task,messages,model='fixture',tid='task',tools=tools,publish=publish)
    publish.assert_called_once()
    event=publish.call_args.args[1]
    assert event['kind']=='state' and event['roundNum']=='final'
    assert event['messages'][-1]['content']=='answer' and event['tools']==tools
    assert 'QUJD' not in str(event['messages'])
    assert messages==before and task['status']=='running'


def test_snapshot_publication_failure_does_not_persist_or_terminalize():
    task={'id':'task','_userId':7,'status':'running'}
    publish=Mock(side_effect=OSError('publication unavailable'))
    with patch('lib.tasks_pkg.manager.persist_task_result') as persist:
        publish_final_messages_snapshot(task,[{'role':'assistant','content':'answer'}],
            model='fixture',tid='task',tools=[],publish=publish)
    publish.assert_called_once();persist.assert_not_called()
    assert task['status']=='running'
