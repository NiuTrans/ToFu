"""Real picker dispatch through served production UI beyond the old size gate.

Only the analysis HTTP result is controlled; the picker action, FormData,
production transport, locale and attachment rendering are served build code.
A metadata-sized File avoids retaining a 513 MiB synthetic browser allocation;
real large source bytes are exercised in the server transport regression.
"""
import json
import pytest
from tests.visual.test_visual_surfaces import _wait_ready

pytestmark = [pytest.mark.visual,pytest.mark.slow]


def test_picker_admits_video_above_former_size_gate_and_renders_attachment(page):
    received = []
    def upload(route):
        received.append(route.request.post_data_buffer)
        attachment = {'attachmentId':'video-demand','kind':'video','name':'demand.mp4',
            'status':'ready','sizeBytes':513*1024*1024,'sourceUrl':'/api/v4/media/attachments/video-demand/source'}
        route.fulfill(status=200,content_type='application/json',body=json.dumps({
            'ok':True,'video_id':'video-demand','status':'ready','attachment':attachment}))
    page.route('**/api/v4/videos/upload',upload)
    _wait_ready(page)
    page.evaluate('''() => {
        const file = new File([new Uint8Array(2048)],'demand.mp4',{type:'video/mp4'});
        const transfer = new DataTransfer(); transfer.items.add(file);
        const picker = document.getElementById('fileInput');
        picker.files = transfer.files;
        Object.defineProperty(picker.files[0],'size',{value:513*1024*1024});
        picker.dispatchEvent(new Event('change',{bubbles:true}));
    }''')
    page.locator('.video-chip[title="demand.mp4"]').wait_for(state='visible')
    page.wait_for_function('''() => document.querySelector('.video-chip') &&
        !document.querySelector('.video-chip.video-failed') &&
        !document.querySelector('.video-chip .img-processing-overlay')''')
    assert len(received) == 1
    assert b'demand.mp4' in received[0]
    assert page.locator('.video-chip.video-failed').count() == 0
