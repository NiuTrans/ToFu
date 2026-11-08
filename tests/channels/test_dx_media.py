"""大象 media (C9): images, files and voice reach the receiving conversation.

Payloads follow the Web SDK packet the bridge forwards (msgId/fromUid/toGuid,
``message`` = the typed body of 消息类型与消息体字段) and the engine's
compacted history rows. Downloads go through a fake media port, attachment
storage through a recorder and speech recognition through its existing seam,
so these specs need no network, storage sidecar or model.
"""
import json
from pathlib import Path

import pytest

from lib.channels.dx.adapter import DxAdapter, event_from_history, event_from_live
from lib.channels.ingress import ingest
from lib.channels.model import DX, ChannelMedia, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

GROUP = 'dx:gid:500'
COMMAND = 'dx:gid:900'
FILE_HOST = DX['fileUrlPrefixes'][0]
IMAGE_URL = FILE_HOST + 'pan/im/4/image/AwUFrscUpfSrY/1280c'
FILE_URL = FILE_HOST + 'pan/im/4/file/ALbKhO6urceg-pQHJr7Tzgn6dWzuWqdoyQ'
VOICE_URL = FILE_HOST + 'pan/im/2/audio/KbvO2_IRCmKg'
RICH_IMAGE_URL = FILE_HOST + 'pan/im/4/image/Ayha-zcFCEa6tWMn/1ea4e836?w=80&h=80'
FILE_TOKEN = 'e9b6e646f802a28d4533f3c21082ec4eac7df09f'  # synthetic file token


def packet(mid, message_type, body, *, sender='2002', gid='500', extension=None):
    """One IM push as the bridge forwards it (the SDK's IMSendGroupMsgReq fields)."""
    return {'msgId': mid, 'msgUuid': f'uuid-{mid}', 'fromUid': sender, 'fromName': f'user{sender}',
            'toGuid': gid, 'groupName': 'Team', 'type': message_type, 'cts': 1_761_000_000_000,
            'message': {'uri': 26279945, 'appid': 0, **body},
            'extension': json.dumps(extension or {})}


def live(mid, message_type, body, **options):
    return event_from_live(packet(mid, message_type, body, **options), source='im', self_uid=OWNER_UID)


IMAGE = {'thumbnail': IMAGE_URL + '?t=t', 'normal': IMAGE_URL + '?t=n', 'original': IMAGE_URL,
         'type': 'jpg', 'token': FILE_TOKEN, 'originSize': 2048, 'uploadImageType': 2, 'custom': ''}
FILE = {'id': '', 'url': FILE_URL, 'name': '季度复盘.pptx',
        'format': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'size': 4096, 'token': FILE_TOKEN, 'lsize': 0, 'custom': ''}
VOICE = {'url': VOICE_URL, 'codec': 1, 'duration': 3, 'stamp': 1_616_723_877_675, 'token': FILE_TOKEN,
         'custom': ''}
RICH = {'type': DX['generalRichText'], 'summary': '', 'data': json.dumps({'nodes': [
    {'t': 'text', 'c': '看看这张图\n'},
    {'t': 'img', 'url': RICH_IMAGE_URL + '&t=b', 'type': 'image/png', 'thumb': RICH_IMAGE_URL + '&t=t',
     'origin': RICH_IMAGE_URL, 'w': 80, 'h': 80, 'originSize': 888},
    {'t': 'link', 'c': '学城', 'url': 'https://km.internal.example.com/page/1'},
    {'t': 'ul', 'children': [[{'t': 'text', 'c': 'Line1'}], [{'t': 'text', 'c': 'Line2'}]]},
]}, ensure_ascii=False)}


# ── normalization ─────────────────────────────────────────────────────

def test_media_messages_become_readable_events_with_media_facts():
    image = live('7000000000000000101', 4, IMAGE)
    assert image.text == '[image]' and image.message_type == 4
    [photo] = image.media
    assert (photo.kind, photo.mime_type, photo.size_bytes) == ('image', 'image/jpeg', 2048)
    assert photo.name == 'image-7000000000000000101.jpg'
    assert (photo.source, photo.token, photo.status) == (IMAGE_URL, FILE_TOKEN, 'pending')

    document = live('7000000000000000102', 8, FILE)
    assert document.text == '[file: 季度复盘.pptx]'
    [deck] = document.media
    assert (deck.kind, deck.name, deck.mime_type, deck.size_bytes) == (
        'file', '季度复盘.pptx', FILE['format'], 4096)

    voice = live('7000000000000000103', 2, VOICE)
    assert voice.text == '[voice message: 3 s]'
    [clip] = voice.media
    assert (clip.kind, clip.name, clip.mime_type, clip.duration_s) == (
        'voice', 'voice-7000000000000000103.amr', DX['voiceMimeType'], 3)

    rich = live('7000000000000000104', 17, RICH)
    assert rich.text == '看看这张图\n[image]学城 (https://km.internal.example.com/page/1)\n- Line1\n- Line2'
    [inline] = rich.media
    assert (inline.kind, inline.mime_type, inline.source) == ('image', 'image/png', RICH_IMAGE_URL)
    assert inline.name == 'image-7000000000000000104-1.png'


@pytest.mark.parametrize('message_type, body, text', [
    (3, {'videoUrl': FILE_HOST + 'v', 'screenshotUrl': FILE_HOST + 'v@jpg', 'duration': 4015,
         'size': 293411, 'width': 288, 'height': 352, 'timestamp': 0},
     '[video: 4 s, 288x352, 286.5 KB]'),
    (19, {'category': 'custom', 'packageId': '1', 'id': 'e1', 'name': '收到', 'type': '', 'params': '{}'},
     '[sticker: 收到]'),
    (11, {'category': '小团', 'name': '开心', 'type': 'jpg'}, '[sticker: 开心]'),
    (6, {'title': '张三提交的报销申请', 'image': 'https://s3plus.yourprovider.net/x.png', 'content': '餐补报销',
         'link': 'https://oa.example/approval/1'},
     '[link: 张三提交的报销申请] https://oa.example/approval/1\n餐补报销'),
    (14, {'title': '放假通知', 'image': '', 'content': '国庆放假安排', 'link': 'https://km.example/notice'},
     '[announcement: 放假通知] https://km.example/notice\n国庆放假安排'),
    (9, {'name': '恒电大厦', 'latitude': 40.00694444444444, 'longitude': 116.48194444444445},
     '[location: 恒电大厦, 40.006944, 116.481944]'),
    (10, {'uid': '7123848', 'name': '熊险峰', 'account': '', 'type': 1, 'subtype': 1},
     '[contact card: 熊险峰]'),
    (12, {'type': '', 'text': 'XXX撤回了一条消息'}, '[system message] XXX撤回了一条消息'),
    (13, {'templateName': '新的代办事项', 'contentTitle': '每日19点30分', 'content': '请及时填写每日工时日志',
          'linkName': '执行完成', 'link': 'https://oa.example/todo'},
     '[template message: 新的代办事项] 每日19点30分\n请及时填写每日工时日志\n执行完成: https://oa.example/todo'),
    (17, {'type': 12, 'summary': '审批通知', 'data': 'eyJ4IjoxfQ=='}, '[card: 审批通知]'),
    (17, {'type': 12, 'summary': '', 'data': ''}, '[card]'),
    (17, {'type': 1, 'summary': '', 'data': {'creator': {'uid': '2002'}, 'callType': 1}}, '[call record]'),
    (17, {'type': 4, 'summary': '投票结果', 'data': '{"x":1}'}, '[general message: 投票结果]'),
    (18, {'guid': 65507720509, 'name': '大象后端测试群', 'account': '', 'type': 3}, '[group card: 大象后端测试群]'),
    (16, {'rpid': 'r1', 'type': 1, 'greetings': '恭喜发财'}, '[red packet: 恭喜发财]'),
    (21, {'id': 'd1', 'title': '周报', 'dxData': '', 'appData': ''}, '[dynamic card: 周报]'),
    (99, {}, '[message type 99]'),
])
def test_other_types_are_described_by_type_name_and_metadata(message_type, body, text):
    event = live('7000000000000000110', message_type, body)
    assert event.text == text and event.media == ()
    assert '[type:' not in event.text


def test_rich_text_data_may_arrive_base64_encoded_as_on_the_send_api():
    import base64
    encoded = base64.b64encode(RICH['data'].encode('utf-8')).decode('ascii')
    event = live('7000000000000000111', 17, {**RICH, 'data': encoded})
    assert event.text.startswith('看看这张图') and len(event.media) == 1


def test_compacted_history_rows_keep_their_media():
    """The engine's history rows drop image URL fields but keep the original in
    the preview text; files keep url/name/format/size; voice keeps its url."""
    thread = ThreadKey.parse(GROUP)

    def row(mid, message_type, body, text):
        raw = {'mid': mid, 'from': '2002', 'fromName': 'Ann', 'cts': 1, 'type': message_type,
               'message': {**body, 'type': message_type, '__type': message_type}, 'extension': '{}'}
        return {'id': mid, 'text': text, 'raw': raw}

    image = event_from_history(row('7000000000000000201', 4, {'originSize': 2048}, f'[image] {IMAGE_URL}'),
                               thread)
    assert image.text == '[image]' and image.media[0].source == IMAGE_URL
    document = event_from_history(row('7000000000000000202', 8, {
        'url': FILE_URL, 'name': 'notes.md', 'format': 'text/markdown', 'size': 12}, FILE_URL), thread)
    assert document.text == '[file: notes.md]'
    assert (document.media[0].source, document.media[0].mime_type) == (FILE_URL, 'text/markdown')
    # The engine's row text is the media URL for voice and files: it never becomes event text.
    voice = event_from_history(row('7000000000000000203', 2, {'url': VOICE_URL}, VOICE_URL), thread)
    assert voice.text == '[voice message]' and voice.media[0].source == VOICE_URL
    rich = event_from_history(row('7000000000000000204', 17, {
        'data': RICH['data'], 'summary': '', 'generalType': DX['generalRichText']}, '[card]'), thread)
    assert rich.text.startswith('看看这张图') and rich.media[0].source == RICH_IMAGE_URL


@pytest.mark.parametrize('name, expected', [
    ('../../etc/passwd', 'passwd'),
    ('..\\..\\Windows\\evil.pptx', 'evil.pptx'),
    ('..', 'file-7000000000000000301'),
    ('.bashrc', 'bashrc'),
    ('re\x00port\x07.pdf', 'report.pdf'),
    ('', 'file-7000000000000000301'),
])
def test_file_names_are_sanitized_against_path_traversal(name, expected):
    event = live('7000000000000000301', 8, {**FILE, 'name': name})
    assert event.media[0].name == expected


def test_source_urls_and_file_tokens_are_never_recorded_or_shown():
    for event in (live('7000000000000000401', 4, IMAGE), live('7000000000000000402', 8, FILE)):
        exposed = repr(event) + json.dumps(event.to_record(), ensure_ascii=False)
        assert FILE_HOST not in exposed and FILE_TOKEN not in exposed


# ── ingress and delivery ──────────────────────────────────────────────

class FakePort:
    """The adapter's media port over a fake media server: URL → bytes or failure."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def download_media(self, item, path):
        self.calls.append((item.kind, item.name, item.token))
        found = self.files.get(item.source)
        if isinstance(found, bytes):
            Path(path).write_bytes(found)
            return {'ok': True, 'contentType': 'application/octet-stream'}
        return {'ok': False, 'detail': found or 'download failed (http_404)'}


@pytest.fixture
def attachments(monkeypatch):
    """Records what reaches the owner's attachment store; returns canonical refs."""
    stored = []

    def ingest_received_file(path, filename, mime_type, *, user_id, origin):
        data = Path(path).read_bytes()
        attachment_id = f'a{len(stored) + 1}'
        stored.append({'name': filename, 'mime': mime_type, 'bytes': data, 'owner': user_id,
                       'origin': origin})
        return {'attachmentId': attachment_id, 'kind': 'document', 'name': filename, 'status': 'ready',
                'mimeType': mime_type, 'sizeBytes': len(data),
                'sourceUrl': f'/api/v4/media/attachments/{attachment_id}/source'}
    monkeypatch.setattr('lib.media_attachments.ingest_received_file', ingest_received_file)
    return stored


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox, attachments):
    enabled(command_chat=COMMAND)
    store.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'mode': 'watch'})
    return store


def test_an_image_from_another_person_is_attached_as_attributed_nonhuman_evidence(channel, dispatch,
                                                                              attachments, records):
    port = FakePort({IMAGE_URL: b'\xff\xd8\xff-jpeg'})
    result = ingest(OWNER, live('7000000000000000501', 4, IMAGE), store=channel, media=port)
    assert (result.outcome, result.disposition) == ('recorded', 'candidate')
    assert port.calls == [('image', 'image-7000000000000000501.jpg', FILE_TOKEN)]
    assert attachments[0]['bytes'] == b'\xff\xd8\xff-jpeg' and attachments[0]['owner'] == OWNER
    message = dispatch.to(ROUTER)[0]['message']
    assert message['origin']['initiator'] == 'channel' and message['_initiator'] == 'channel'
    assert message['attachments'] == [{
        'attachmentId': 'a1', 'kind': 'document', 'name': 'image-7000000000000000501.jpg',
        'status': 'ready', 'mimeType': 'image/jpeg', 'sizeBytes': 8,
        'sourceUrl': '/api/v4/media/attachments/a1/source'}]
    content = message['content']
    block = content[content.index('<channel-event '):]
    assert 'sender="user2002"' in block and 'owner_authored="false"' in block
    assert ('<channel-media kind="image" name="image-7000000000000000501.jpg" mime_type="image/jpeg" '
            'size_bytes="8" status="attached" attachment_ref="att_media_a1"/>') in block
    recorded = channel.event(ThreadKey.parse(GROUP), '7000000000000000501')
    assert recorded['media'][0]['status'] == 'attached'
    assert recorded['media'][0]['attachment']['attachmentId'] == 'a1'
    everything = (json.dumps(list(records.rows.values()), ensure_ascii=False)
                  + json.dumps(dispatch.calls, ensure_ascii=False))
    assert FILE_HOST not in everything and FILE_TOKEN not in everything


def test_an_owner_file_in_the_command_chat_is_human_input_with_its_attachment(channel, dispatch,
                                                                            attachments):
    port = FakePort({FILE_URL: b'PK\x03\x04deck'})
    event = live('7000000000000000502', 8, FILE, sender=OWNER_UID, gid='900')
    assert ingest(OWNER, event, store=channel, media=port).disposition == 'command'
    message = dispatch.to(ROUTER)[0]['message']
    assert message['origin']['initiator'] == 'human' and '_initiator' not in message
    assert message['content'] == '[file: 季度复盘.pptx]\n[file attached: 季度复盘.pptx — att_media_a1]'
    assert [item['attachmentId'] for item in message['attachments']] == ['a1']
    assert attachments[0]['mime'] == FILE['format']


def test_plain_text_command_messages_stay_verbatim(channel, dispatch):
    text = '帮我总结一下\n  第二行'
    event = live('7000000000000000503', 1, {'text': text}, sender=OWNER_UID, gid='900')
    ingest(OWNER, event, store=channel, media=FakePort({}))
    message = dispatch.to(ROUTER)[0]['message']
    assert message['content'] == text and 'attachments' not in message


@pytest.fixture
def speech(monkeypatch):
    calls = []

    def transcribe_track(path, scratch_dir, duration_s, *, owner_user_id=None, tenant_id=None):
        calls.append({'audio': Path(path).read_bytes(), 'duration': duration_s, 'owner': owner_user_id})
        return {'text': '帮我看一下这个PPT', 'status': 'ok', 'model': 'speech-test'}
    monkeypatch.setattr('lib.video_analysis.transcribe_track', transcribe_track)
    return calls


def test_voice_is_transcribed_beside_its_audio_when_speech_recognition_is_configured(
        channel, dispatch, attachments, speech):
    port = FakePort({VOICE_URL: b'#!AMR\n\x3c'})
    ingest(OWNER, live('7000000000000000504', 2, VOICE, sender=OWNER_UID, gid='900'), store=channel,
           media=port)
    assert speech == [{'audio': b'#!AMR\n\x3c', 'duration': 3.0, 'owner': OWNER}]
    assert attachments[0]['mime'] == 'audio/amr'
    message = dispatch.to(ROUTER)[0]['message']
    assert message['content'] == ('[voice message: 3 s]\n'
                                  '[voice attached: voice-7000000000000000504.amr — att_media_a1]\n'
                                  '[transcript] 帮我看一下这个PPT')
    recorded = channel.event(ThreadKey.parse(COMMAND), '7000000000000000504')['media'][0]
    assert (recorded['transcript'], recorded['transcriptStatus']) == ('帮我看一下这个PPT', 'ok')


def test_voice_without_speech_recognition_keeps_the_audio_and_says_so(channel, dispatch, attachments,
                                                                     monkeypatch):
    monkeypatch.setattr('lib.transcription.transcription_available', lambda **kwargs: False)
    port = FakePort({VOICE_URL: b'#!AMR\n\x3c'})
    ingest(OWNER, live('7000000000000000505', 2, VOICE), store=channel, media=port)
    content = dispatch.to(ROUTER)[0]['message']['content']
    assert 'status="attached" attachment_ref="att_media_a1"' in content
    assert ('transcript_status="unavailable" transcript_detail="no transcription model is configured"'
            in content)
    assert len(attachments) == 1


def test_a_failed_download_is_reported_in_the_turn_text(channel, dispatch, attachments):
    port = FakePort({FILE_URL: 'download failed (http_403)'})
    ingest(OWNER, live('7000000000000000506', 8, FILE), store=channel, media=port)
    message = dispatch.to(ROUTER)[0]['message']
    assert 'attachments' not in message and attachments == []
    assert ('<channel-media kind="file" name="季度复盘.pptx" mime_type="'
            + FILE['format'] + '" size_bytes="4096" status="failed" detail="download failed (http_403)"/>'
            ) in message['content']
    owner_file = live('7000000000000000507', 8, FILE, sender=OWNER_UID, gid='900')
    ingest(OWNER, owner_file, store=channel, media=port)
    assert dispatch.to(ROUTER)[1]['message']['content'] == (
        '[file: 季度复盘.pptx]\n[file not attached: 季度复盘.pptx — download failed (http_403)]')


def test_media_of_dropped_or_ignored_messages_is_never_downloaded(channel, dispatch):
    port = FakePort({IMAGE_URL: b'img'})
    channel.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'mode': 'mentions'})
    assert ingest(OWNER, live('7000000000000000508', 4, IMAGE), store=channel, media=port).outcome == 'dropped'
    assert ingest(OWNER, live('7000000000000000509', 4, IMAGE, gid='777'), store=channel,
                  media=port).outcome == 'ignored'
    assert port.calls == [] and dispatch.calls == []


def test_a_replay_reuses_the_recorded_media_instead_of_downloading_again(channel, dispatch, attachments):
    from lib.channels.delivery import DeliveryFailed
    port = FakePort({FILE_URL: b'%PDF-1.7'})
    event = live('7000000000000000510', 8, FILE)
    dispatch.outcomes[ROUTER] = 'start_failed'
    with pytest.raises(DeliveryFailed):
        ingest(OWNER, event, store=channel, media=port)
    dispatch.outcomes[ROUTER] = 'started'
    replayed = ingest(OWNER, event, store=channel, media=port)
    assert replayed.outcome == 'recorded'
    assert len(port.calls) == 1 and len(attachments) == 1
    assert [item['attachmentId'] for item in dispatch.calls[-1]['message']['attachments']] == ['a1']


def test_live_pushes_and_catch_up_replays_fetch_media_through_the_session_bridge(channel, dispatch,
                                                                                attachments):
    from tests.channels.test_dx_adapter import FakeBridge, _run, mid_at

    class MediaBridge(FakeBridge):
        files = {FILE_URL: b'notes', IMAGE_URL: b'\xff\xd8\xffimg'}

        def request(self, op, **args):
            if op != 'download':
                return super().request(op, **args)
            self.requests.append((op, args))
            Path(args['path']).write_bytes(self.files[args['url']])
            return {'ok': True, 'result': {'bytes': len(self.files[args['url']]),
                                           'contentType': 'application/octet-stream'}}

    channel.update_account('dx', lambda account: {**account, 'frontierAtMs': 1_761_000_000_000})
    channel.update_thread(ThreadKey.parse(GROUP),
                          lambda value: {**value, 'watermark': mid_at(1_761_000_001_000)})
    missed = mid_at(1_761_000_002_000)
    row = {'id': missed, 'text': FILE_URL, 'raw': {
        'mid': missed, 'from': '2002', 'fromName': 'Ann', 'cts': 1, 'type': 8, 'extension': '{}',
        'message': {'url': FILE_URL, 'name': 'notes.md', 'format': 'text/markdown', 'size': 5, 'type': 8}}}
    bridge = MediaBridge([{'type': 'group', 'id': '500', 'name': 'Team',
                           'lastMessageServerTime': 1_761_000_002_000}], {GROUP: [row]})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    pushed = mid_at(1_761_000_003_000)
    _run(adapter, bridge, {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID},
         {'type': 'message', 'source': 'im', 'message': packet(pushed, 4, IMAGE)})
    adapter.stop(1)
    assert [args['url'] for op, args in bridge.requests if op == 'download'] == [FILE_URL, IMAGE_URL]
    replayed, live_push = (call['message'] for call in dispatch.to(ROUTER))
    assert [item['name'] for item in replayed['attachments']] == ['notes.md']
    assert [item['name'] for item in live_push['attachments']] == [f'image-{pushed}.jpg']
    assert FILE_HOST not in replayed['content'] + live_push['content']


# ── the adapter's media port ──────────────────────────────────────────

class RecordingBridge:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def request(self, op, **args):
        self.requests.append((op, args))
        return self.answer


def test_the_port_fetches_only_dx_file_hosts_through_the_bridge(channel, tmp_path):
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: None)
    adapter._bridge = RecordingBridge({'ok': True, 'result': {'bytes': 4, 'contentType': 'image/jpeg'}})
    path = str(tmp_path / 'one.bin')
    item = ChannelMedia('image', 'image-1.jpg', source=IMAGE_URL, token=FILE_TOKEN)
    assert adapter.download_media(item, path) == {'ok': True, 'contentType': 'image/jpeg'}
    [(op, args)] = adapter._bridge.requests
    assert op == 'download' and args == {'url': IMAGE_URL, 'token': FILE_TOKEN, 'path': path,
                                         'credentialPrefixes': list(DX['fileUrlPrefixes'])}

    adapter._bridge.requests.clear()
    outside = ChannelMedia('file', 'x.pdf', source='https://evil.example/pan/im/4/file/x?token=s3cret')
    refused = adapter.download_media(outside, path)
    assert refused['ok'] is False and 'evil.example' in refused['detail'] and 's3cret' not in refused['detail']
    assert adapter._bridge.requests == []
    assert adapter.download_media(ChannelMedia('image', 'image-2.jpg'), path)['ok'] is False


def test_the_port_reports_bridge_refusals_and_login_pages(channel, tmp_path):
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: None)
    path = str(tmp_path / 'one.bin')
    item = ChannelMedia('file', 'deck.pptx', source=FILE_URL)
    adapter._bridge = RecordingBridge({'ok': False, 'error': {'code': 'http_403', 'message': 'HTTP 403'}})
    assert adapter.download_media(item, path) == {'ok': False, 'detail': 'download failed (http_403)'}
    adapter._bridge = RecordingBridge({'ok': True, 'result': {'bytes': 900, 'contentType': 'text/html; charset=utf-8'}})
    page = adapter.download_media(item, path)
    assert page['ok'] is False and 'web page' in page['detail']
