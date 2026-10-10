"""Native response provider evidence comes only from completed dispatch metadata."""
import pytest
from routes.api_v1.chat import _completion_response
pytestmark=pytest.mark.unit


def test_response_surfaces_last_dispatch_provider_without_requested_provider_guess():
    task={'id':'task','status':'done','finishReason':'stop','content':'original answer',
          'provider_id':'actual-fallback','config':{'provider_id':'requested'}}
    result=_completion_response(task,model='requested-model')
    assert result['provider_id']=='actual-fallback'
    assert result['choices'][0]['message']['content']=='original answer'


@pytest.mark.parametrize('provider',[None,123,{},'x'*161])
def test_unknown_or_invalid_provider_does_not_become_an_identity(provider):
    task={'id':'task','status':'done','finishReason':'stop','content':'answer',
          'provider_id':provider,'config':{'provider_id':'requested'}}
    assert _completion_response(task,model='requested-model')['provider_id']==''
