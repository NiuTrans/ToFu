"""Local knowledge-base ingestion, retrieval and conditional tool exposure."""

from __future__ import annotations

import io
import zipfile

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_knowledge(tmp_path, monkeypatch):
    from lib.knowledge import store
    monkeypatch.setattr(store, '_DB_PATH_OVERRIDE', str(tmp_path / 'knowledge.sqlite3'))
    monkeypatch.setattr(store, '_SOURCE_ROOT_OVERRIDE', str(tmp_path / 'sources'))
    yield store


def test_empty_or_disabled_corpus_contributes_no_model_tool(isolated_knowledge):
    from lib.knowledge.tool import build_tool
    assert build_tool(None) == []

    isolated_knowledge.add_document(
        '报销制度：差旅发票应在三十天内提交。'.encode(), 'policy.txt')
    assert [t['function']['name'] for t in build_tool(None)] == ['search_knowledge']

    isolated_knowledge.set_enabled(False)
    assert build_tool(None) == []


def test_status_read_does_not_initialize_an_incomplete_store(
        isolated_knowledge, tmp_path):
    db_path = tmp_path / 'knowledge.sqlite3'
    db_path.touch()

    status = isolated_knowledge.get_status()

    assert status['available'] is False
    assert status['totals']['documents'] == 0
    assert db_path.stat().st_size == 0
    assert list(tmp_path.glob('knowledge.sqlite3-*')) == []


def test_catalog_only_lists_knowledge_tool_while_available(isolated_knowledge):
    from lib.tools.registry._introspect import build_tool_inventory

    def knowledge_families():
        inventory = build_tool_inventory()
        return [
            family
            for group in inventory['groups']
            for family in group['families']
            if family['key'] == 'knowledge'
        ]

    assert knowledge_families() == []
    isolated_knowledge.add_document(b'local evidence', 'evidence.txt')
    families = knowledge_families()
    assert len(families) == 1
    assert [row['name'] for row in families[0]['tools']] == [
        'search_knowledge']

    isolated_knowledge.set_enabled(False)
    assert knowledge_families() == []


def test_knowledge_tool_has_human_readable_activity_labels():
    from lib.tasks_pkg.tool_dispatch._labels import tool_label
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_label('search_knowledge') == 'Searching local knowledge'
    label = tool_round_label('search_knowledge', {'query': '报销制度'})
    assert label == 'Searching local knowledge: 报销制度'


def test_plaintext_is_deduplicated_and_cjk_search_is_grounded(isolated_knowledge):
    raw = '研发部张三擅长边缘计算。\n年假为十天。'.encode('utf-16')
    first = isolated_knowledge.add_document(raw, '无扩展知识')
    second = isolated_knowledge.add_document(raw, 'renamed.txt')
    assert first['kind'] == '.txt'
    assert second['duplicate'] is True
    assert isolated_knowledge.get_status()['totals']['documents'] == 1

    from lib.knowledge.search import search
    hits = search('谁擅长边缘计算')
    assert hits
    assert hits[0]['source'] == '无扩展知识'
    assert '张三' in hits[0]['excerpt']


def test_bold_pdf_headings_and_install_intent_retrieve_each_setup_item(
        isolated_knowledge):
    handbook = '''# **办公电脑配置指引**

**一、大象 — 与公司同事进行业务沟通**

统一通讯软件大象，公司电脑默认安装客户端。

**二、邮件系统 — 查看公司邮件**

网页版邮箱或大象 App 均可使用，电脑邮箱客户端请按手册配置。

**三、Zoom — 召开或参与视频会议**

Zoom 客户端需要完成安装和 SSO 登录。

**四、打印机 — 公司打印机安装及配置**

请在电脑上添加打印机并完成驱动配置。
'''
    document = isolated_knowledge.add_document(
        handbook.encode(), '新员工入职IT指南.md')
    assert document['chunk_count'] == 5

    parsed = isolated_knowledge.get_document_content(document['id'])
    assert parsed is not None
    assert [chunk['section'] for chunk in parsed['chunks']][1:] == [
        '一、大象 — 与公司同事进行业务沟通',
        '二、邮件系统 — 查看公司邮件',
        '三、Zoom — 召开或参与视频会议',
        '四、打印机 — 公司打印机安装及配置',
    ]

    from lib.knowledge.search import search
    hits = search('员工需要安装什么软件', limit=6)
    excerpts = '\n'.join(hit['excerpt'] for hit in hits)
    for expected in ('大象', '邮件系统', 'Zoom', '打印机'):
        assert expected in excerpts


def test_install_intent_expansion_participates_in_candidate_recall(
        isolated_knowledge):
    isolated_knowledge.add_document(
        '星云桌面客户端由信息技术部门统一下发。'.encode(), '入职说明.txt')

    from lib.knowledge.search import search
    hits = search('员工要安装什么软件')

    assert hits
    assert '星云桌面客户端' in hits[0]['excerpt']


def test_excel_magic_wins_over_suffix_and_sparse_table_blocks_survive(
        isolated_knowledge):
    openpyxl = pytest.importorskip('openpyxl')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '研发 人员（华东）'
    ws.append(['姓名', '特长'])
    ws.append(['张三', '边缘计算'])
    for _ in range(70):
        ws.append([None, None])
    ws.append(['制度', '天数'])
    ws.append(['年假', 10])
    table = openpyxl.worksheet.table.Table(
        displayName='Policy_Table_2026', ref='A73:B74')
    ws.add_table(table)
    buf = io.BytesIO()
    wb.save(buf)

    doc = isolated_knowledge.add_document(buf.getvalue(), 'not-really.txt')
    assert doc['kind'] == '.xlsx'
    from lib.knowledge.search import search
    hits = search('Policy Table 2026 年假天数')
    assert any('年假' in hit['excerpt'] for hit in hits)
    assert any('Policy_Table_2026' in hit['excerpt'] for hit in hits)
    assert any(hit['section'] == 'Table block 2' for hit in hits)


def test_unformatted_docx_and_misleading_suffix(isolated_knowledge):
    docx = pytest.importorskip('docx')
    document = docx.Document()
    document.add_paragraph('没有标题样式的操作说明。')
    document.add_paragraph('紧急联系人是李雷，分机 8848。')
    buf = io.BytesIO()
    document.save(buf)
    indexed = isolated_knowledge.add_document(buf.getvalue(), 'notes.bin')
    assert indexed['kind'] == '.docx'
    from lib.knowledge.search import search
    assert '李雷' in search('紧急联系人分机')[0]['excerpt']


def test_delete_last_document_makes_tool_unavailable(isolated_knowledge):
    doc = isolated_knowledge.add_document(b'alpha beta gamma', 'a.md')
    assert isolated_knowledge.tool_available()
    assert isolated_knowledge.delete_document(doc['id'])
    assert not isolated_knowledge.tool_available()
    assert isolated_knowledge.delete_document(doc['id']) is False


def test_reindex_atomically_replaces_searchable_content(
        isolated_knowledge, monkeypatch):
    doc = isolated_knowledge.add_document(
        b'obsolete-needle-54321', 'handbook.txt')
    monkeypatch.setattr(isolated_knowledge, 'extract', lambda _raw, _name: {
        'text': '# Revised\n\nnew canonical value 7788',
        'kind': '.txt',
        'method': 'test-upgraded-parser',
        'warnings': ['parser upgraded'],
        'pages': 1,
    })

    updated = isolated_knowledge.reindex_document(doc['id'])

    assert updated['method'] == 'test-upgraded-parser'
    assert updated['warnings'] == ['parser upgraded']
    from lib.knowledge.search import search
    assert '7788' in search('canonical value 7788')[0]['excerpt']
    assert search('obsolete-needle-54321') == []


def test_upload_respects_an_explicit_disabled_choice(isolated_knowledge):
    isolated_knowledge.set_enabled(False)
    isolated_knowledge.add_document(b'private draft evidence', 'draft.txt')
    status = isolated_knowledge.get_status()
    assert status['totals']['documents'] == 1
    assert status['enabled'] is False
    assert status['available'] is False


def test_html_extraction_discards_executable_noise(isolated_knowledge):
    raw = b'''<!doctype html><html><head><style>.secret{}</style></head>
    <body><h1>Incident guide</h1><p>Escalation code is BLUE-42.</p>
    <script>var misleading = "WRONG-99";</script></body></html>'''
    isolated_knowledge.add_document(raw, 'guide.html')
    from lib.knowledge.search import search
    assert 'BLUE-42' in search('escalation code')[0]['excerpt']
    assert search('WRONG-99') == []


def test_rtf_unicode_and_email_attachment_are_searchable(isolated_knowledge):
    rtf = rb'{\rtf1\ansi Policy: submit in \u19971? days.\par Owner: Alice}'
    isolated_knowledge.add_document(rtf, 'policy.rtf')

    eml = (b'Subject: Operations handbook\r\nFrom: ops@example.com\r\n'
           b'To: team@example.com\r\nMIME-Version: 1.0\r\n'
           b'Content-Type: multipart/mixed; boundary="x"\r\n\r\n'
           b'--x\r\nContent-Type: text/plain\r\n\r\nSee the attachment.\r\n'
           b'--x\r\nContent-Type: text/plain\r\n'
           b'Content-Disposition: attachment; filename="contacts.txt"\r\n\r\n'
           b'Night contact: Charlie 7788\r\n--x--\r\n')
    isolated_knowledge.add_document(eml, 'message.eml')
    from lib.knowledge.search import search
    assert any('七' in hit['excerpt'] for hit in search('submit 七 days'))
    assert any('Charlie 7788' in hit['excerpt'] for hit in search('Night contact'))


def test_opendocument_magic_wins_over_name(isolated_knowledge):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr(
            'mimetype', 'application/vnd.oasis.opendocument.text',
            compress_type=zipfile.ZIP_STORED)
        archive.writestr('content.xml', '''
          <office:document-content
            xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
            xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
            <office:body><office:text><text:h>Runbook</text:h>
            <text:p>Failover region is Hangzhou.</text:p></office:text></office:body>
          </office:document-content>''')
    indexed = isolated_knowledge.add_document(buf.getvalue(), 'mystery.bin')
    assert indexed['kind'] == '.odt'
    from lib.knowledge.search import search
    assert 'Hangzhou' in search('failover region')[0]['excerpt']


def test_semicolon_csv_infers_a_header_below_report_context(
        isolated_knowledge):
    raw = '''Q3 onboarding inventory;;
Generated for Beijing;;
Employee;Required software;Owner
Alice;Zoom;IT Service Desk
Bob;Printer driver;Workplace Support
'''.encode()

    document = isolated_knowledge.add_document(raw, 'onboarding.csv')

    assert document['method'] == 'delimited-table-local'
    parsed = isolated_knowledge.get_document_content(document['id'])
    content = '\n'.join(chunk['content'] for chunk in parsed['chunks'])
    assert 'Table context: Q3 onboarding inventory' in content
    assert '| Employee | Required software | Owner |' in content
    from lib.knowledge.search import search
    hits = search('Alice required software owner')
    assert hits
    assert 'Zoom' in hits[0]['excerpt']


def test_table_header_can_follow_a_long_preamble_and_repairs_labels():
    from lib.doc_parser._tables import render_markdown_table
    rows = [[f'Report context line {index}'] for index in range(12)]
    rows.extend((
        ['Employee', '', 'Employee'],
        ['Alice', 'Zoom', 'Workplace IT'],
    ))

    rendered = render_markdown_table(rows)

    assert 'Table context: Report context line 11' in rendered
    assert '| Employee | Column 2 | Employee (2) |' in rendered
    assert '| Alice | Zoom | Workplace IT |' in rendered


def test_pptx_table_cells_are_searchable_with_a_nonfirst_header(
        isolated_knowledge):
    pptx = pytest.importorskip('pptx')
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = slide.shapes.add_table(
        4, 3, pptx.util.Inches(1), pptx.util.Inches(1),
        pptx.util.Inches(8), pptx.util.Inches(3)).table
    values = (
        ('New hire application matrix', '', ''),
        ('Employee', 'Required software', 'Owner'),
        ('Alice', 'Zoom', 'IT Service Desk'),
        ('Bob', 'Printer driver', 'Workplace Support'),
    )
    for row_index, row in enumerate(values):
        for column_index, value in enumerate(row):
            table.cell(row_index, column_index).text = value
    buffer = io.BytesIO()
    presentation.save(buffer)

    document = isolated_knowledge.add_document(
        buffer.getvalue(), 'onboarding.pptx')

    parsed = isolated_knowledge.get_document_content(document['id'])
    content = '\n'.join(chunk['content'] for chunk in parsed['chunks'])
    assert 'Table context: New hire application matrix' in content
    assert '| Employee | Required software | Owner |' in content
    from lib.knowledge.search import search
    assert any('Zoom' in hit['excerpt']
               for hit in search('Alice required software'))


def test_ods_table_uses_the_same_header_inference(isolated_knowledge):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(
            'mimetype',
            'application/vnd.oasis.opendocument.spreadsheet',
            compress_type=zipfile.ZIP_STORED)
        archive.writestr('content.xml', '''
          <office:document-content
            xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
            xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
            xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
            <office:body><office:spreadsheet><table:table table:name="Apps">
              <table:table-row>
                <table:table-cell><text:p>New hire application matrix</text:p></table:table-cell>
                <table:table-cell/><table:table-cell/>
              </table:table-row>
              <table:table-row>
                <table:table-cell><text:p>Employee</text:p></table:table-cell>
                <table:table-cell><text:p>Required software</text:p></table:table-cell>
                <table:table-cell><text:p>Owner</text:p></table:table-cell>
              </table:table-row>
              <table:table-row>
                <table:table-cell><text:p>Alice</text:p></table:table-cell>
                <table:table-cell><text:p>Zoom</text:p></table:table-cell>
                <table:table-cell><text:p>IT Service Desk</text:p></table:table-cell>
              </table:table-row>
              <table:table-row>
                <table:table-cell table:number-columns-repeated="2"/>
                <table:table-cell office:value-type="string"
                  office:string-value="Escalation"/>
              </table:table-row>
            </table:table></office:spreadsheet></office:body>
          </office:document-content>''')

    document = isolated_knowledge.add_document(
        buffer.getvalue(), 'misleading-name.bin')

    assert document['kind'] == '.ods'
    parsed = isolated_knowledge.get_document_content(document['id'])
    content = '\n'.join(chunk['content'] for chunk in parsed['chunks'])
    assert 'Table context: New hire application matrix' in content
    assert '| Employee | Required software | Owner |' in content
    assert '|  |  | Escalation |' in content
    from lib.knowledge.search import search
    assert any('Zoom' in hit['excerpt']
               for hit in search('Alice required software'))


def test_repeated_pdf_margins_are_removed_without_losing_page_content():
    from lib.knowledge.ingest import _strip_repeated_pdf_margins
    text = '''# Shared onboarding guide
Page one unique printer setup
Confidential internal use

---

# Shared onboarding guide
Page two unique Zoom setup
Confidential internal use

---

# Shared onboarding guide
Page three unique mailbox setup
Confidential internal use'''

    cleaned = _strip_repeated_pdf_margins(text)

    assert 'Shared onboarding guide' not in cleaned
    assert 'Confidential internal use' not in cleaned
    assert 'printer setup' in cleaned
    assert 'Zoom setup' in cleaned
    assert 'mailbox setup' in cleaned
    assert cleaned.count('---') == 2


def test_search_suppresses_near_duplicate_evidence_across_documents(
        isolated_knowledge):
    canonical = (
        'NEBULA-7741 access procedure requires the employee to install the '
        'company VPN client, sign in with SSO, and confirm the security '
        'certificate before opening finance systems.')
    isolated_knowledge.add_document(canonical.encode(), 'handbook-a.txt')
    isolated_knowledge.add_document(
        (canonical + '\nReference copy maintained by Workplace IT.').encode(),
        'handbook-b.txt')

    from lib.knowledge.search import search
    hits = search('NEBULA-7741 VPN finance systems', limit=6)

    assert len(hits) == 1
    assert 'company VPN client' in hits[0]['excerpt']


def test_tool_schema_is_minimal():
    from lib.knowledge.tool import SEARCH_KNOWLEDGE_TOOL
    params = SEARCH_KNOWLEDGE_TOOL['function']['parameters']
    assert params['required'] == ['query']
    assert set(params['properties']) == {'query'}
    assert params['additionalProperties'] is False


def test_image_is_durable_searchable_multimodal_evidence(
        flask_client, isolated_knowledge, tmp_path):
    image_module = pytest.importorskip('PIL.Image')
    image = image_module.new('RGB', (96, 64), (35, 120, 210))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    raw = buffer.getvalue()

    document = isolated_knowledge.add_document(raw, 'org-chart-blue.png')
    assert document['kind'] == '.png'
    assert document['asset_count'] == 1
    assert isolated_knowledge.get_status()['totals']['assets'] == 1

    from lib.knowledge.search import search
    results = search('org chart blue')
    assert results and len(results[0]['assets']) == 1
    asset = results[0]['assets'][0]
    assert asset['kind'] == 'image'
    assert 'stored_name' not in asset
    assert 'sha256' not in asset

    response = flask_client.get(asset['url'])
    assert response.status_code == 200
    assert response.data == raw
    assert response.headers['Cache-Control'] == 'private, no-store'
    thumbnail = flask_client.get(asset['thumbnail_url'])
    assert thumbnail.status_code == 200
    assert thumbnail.headers['Content-Type'].split(';', 1)[0] in (
        'image/jpeg', 'image/png')

    from lib.knowledge.tool import _multimodal_results
    payload = _multimodal_results(results)
    assert payload['__screenshot__'] is True
    assert payload['images'][0]['assetId'] == asset['id']
    assert 'org-chart-blue.png' in payload['_no_vision_fallback']

    assert isolated_knowledge.delete_document(document['id']) is True
    assert list((tmp_path / 'assets').glob('*')) == []
    assert flask_client.get(asset['url']).status_code == 404


def test_visual_enrichment_requires_consent_and_atomically_refreshes_search(
        isolated_knowledge, monkeypatch):
    image_module = pytest.importorskip('PIL.Image')
    image = image_module.new('RGB', (80, 50), (240, 210, 30))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    document = isolated_knowledge.add_document(
        buffer.getvalue(), 'unlabeled-diagram.png')

    from lib.database import knowledge_repository as repository
    assets = repository.list_assets(
        isolated_knowledge._db_path(), document_id=document['id'])
    assert assets[0]['enrichment_status'] == 'not_requested'

    from lib.knowledge import enrichment
    monkeypatch.setattr(enrichment, 'start_visual_enrichment', lambda: False)
    isolated_knowledge.set_visual_enrichment(True)
    queued = repository.list_assets(
        isolated_knowledge._db_path(), document_id=document['id'])
    assert queued[0]['enrichment_status'] == 'pending'

    monkeypatch.setattr(enrichment, '_vision_models', lambda: ['vision-test'])
    monkeypatch.setattr(
        enrichment, '_describe',
        lambda _raw, _mime, _row: (
            'A yellow dependency graph labeled ATLAS-NEBULA.', 'vision-test'))
    enrichment._run()

    ready = repository.find_asset_by_id(
        isolated_knowledge._db_path(), assets[0]['id'])
    assert ready['enrichment_status'] == 'ready'
    assert ready['enrichment_model'] == 'vision-test'
    from lib.knowledge.search import search
    hit = search('ATLAS NEBULA')[0]
    assert 'yellow dependency graph' in hit['excerpt']
    assert hit['assets'][0]['description'].startswith('A yellow')


def test_knowledge_images_keep_grounded_text_for_a_text_only_model(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline
    monkeypatch.setattr(
        _pipeline, 'model_supports_vision', lambda _model: False)
    content, no_vision = _pipeline._screenshot_display_content(
        'text-only-test', {
            '__screenshot__': True,
            '_text_fallback': 'visual model text',
            '_no_vision_fallback': 'grounded OCR and caption evidence',
        })
    assert no_vision is True
    assert content == 'grounded OCR and caption evidence'


def test_pdf_figure_asset_retains_page_and_bbox_provenance():
    pymupdf = pytest.importorskip('pymupdf')
    image_module = pytest.importorskip('PIL.Image')
    image = image_module.new('RGB', (240, 120), (72, 160, 98))
    image_buffer = io.BytesIO()
    image.save(image_buffer, format='PNG')

    document = pymupdf.open()
    page = document.new_page(width=420, height=520)
    page.insert_image(
        pymupdf.Rect(60, 70, 360, 220), stream=image_buffer.getvalue())
    page.insert_text((70, 250), 'Figure 1. Green architecture overview')
    raw_pdf = document.tobytes()
    document.close()

    from lib.knowledge.assets import extract_pdf_assets
    assets, warnings = extract_pdf_assets(raw_pdf)
    assert not warnings
    figure = next(asset for asset in assets if asset['kind'] == 'figure')
    assert figure['page'] == 1
    assert figure['pages'] == [1]
    assert len(figure['bbox']) == 4
    assert figure['caption'].startswith('Figure 1')


def test_uncaptioned_pdf_diagram_is_not_lost():
    pymupdf = pytest.importorskip('pymupdf')
    image_module = pytest.importorskip('PIL.Image')
    diagram = image_module.effect_noise((300, 180), 80).convert('RGB')
    image_buffer = io.BytesIO()
    diagram.save(image_buffer, format='JPEG', quality=90)

    document = pymupdf.open()
    page = document.new_page(width=420, height=520)
    page.insert_textbox(
        pymupdf.Rect(35, 25, 385, 75),
        'Architecture discussion with enough body text to represent a normal '
        'digital PDF page rather than a scanned page. The diagram below has '
        'no caption but remains primary visual evidence.', fontsize=10)
    page.insert_image(
        pymupdf.Rect(60, 100, 360, 280), stream=image_buffer.getvalue())
    raw_pdf = document.tobytes()
    document.close()

    from lib.knowledge.assets import extract_pdf_assets
    assets, warnings = extract_pdf_assets(raw_pdf)
    assert not warnings
    embedded = next(
        asset for asset in assets if asset['source'] == 'embedded_pdf')
    assert embedded['page'] == 1
    assert embedded['width'] == 300
    assert embedded['height'] == 180


def test_model_image_payload_is_bounded_without_touching_the_original():
    image_module = pytest.importorskip('PIL.Image')
    noisy = image_module.effect_noise((2200, 1600), 100).convert('RGB')
    source = io.BytesIO()
    noisy.save(source, format='PNG')
    raw = source.getvalue()
    assert len(raw) > 1536 * 1024

    from lib.knowledge.assets import model_ready_image
    prepared, mime = model_ready_image(raw, 'image/png')
    assert mime == 'image/jpeg'
    assert len(prepared) <= 1536 * 1024
    assert raw.startswith(b'\x89PNG')


def test_management_api_upload_toggle_and_delete(flask_client, isolated_knowledge):
    from werkzeug.datastructures import FileStorage

    response = flask_client.post(
        '/api/v1/knowledge/documents',
        form={},
        files={'files': FileStorage(
            stream=io.BytesIO('值班电话：12345'.encode()),
            filename='handbook.md', content_type='text/markdown')},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['available'] is True
    assert payload['totals']['documents'] == 1
    doc_id = payload['indexed'][0]['id']

    response = flask_client.get(
        '/api/v1/knowledge?page=1&page_size=1&category=text&query=hand')
    assert response.status_code == 200
    catalog = response.get_json()
    assert catalog['pagination']['total_items'] == 1
    assert catalog['documents'][0]['category'] == 'text'
    assert {item['category']: item['count']
            for item in catalog['facets']}['text'] == 1

    activity = flask_client.get('/api/v1/knowledge/activity')
    assert activity.status_code == 200
    assert activity.get_json()['pending_assets'] == 0

    response = flask_client.get('/api/v1/knowledge?page_size=101')
    assert response.status_code == 400

    response = flask_client.get(
        f'/api/v1/knowledge/documents/{doc_id}/content?offset=0&limit=1')
    assert response.status_code == 200
    content = response.get_json()
    assert content['document']['id'] == doc_id
    assert content['chunks'][0]['content'] == '值班电话：12345'
    assert content['pagination']['total_items'] == 1
    assert content['pagination']['has_more'] is False

    response = flask_client.post(
        '/api/v1/knowledge/settings', json={'enabled': False})
    assert response.status_code == 200
    assert response.get_json()['available'] is False

    response = flask_client.post(
        '/api/v1/knowledge/search', json={'query': '值班电话'})
    assert response.status_code == 200
    assert response.get_json()['count'] == 1
    assert '12345' in response.get_json()['results'][0]['excerpt']

    response = flask_client.post(
        f'/api/v1/knowledge/documents/{doc_id}/reindex', json={})
    assert response.status_code == 200
    assert response.get_json()['reindexed']['id'] == doc_id

    response = flask_client.delete(
        f'/api/v1/knowledge/documents/{doc_id}')
    assert response.status_code == 200
    assert response.get_json()['totals']['documents'] == 0


def test_management_api_exposes_visual_consent_separately(
        flask_client, isolated_knowledge, monkeypatch):
    from lib.knowledge import enrichment
    monkeypatch.setattr(enrichment, 'start_visual_enrichment', lambda: False)

    initial = flask_client.get('/api/v1/knowledge').get_json()
    assert initial['visual_enrichment'] is False
    assert initial['privacy'] == 'local_only'

    response = flask_client.post(
        '/api/v1/knowledge/settings', json={'visual_enrichment': True})
    assert response.status_code == 200
    enabled = response.get_json()
    assert enabled['visual_enrichment'] is True
    assert enabled['privacy'] == 'local_with_opt_in_visual_provider'
    assert enabled['visual_enrichment_sends_images_to_configured_provider'] is True

    response = flask_client.post(
        '/api/v1/knowledge/settings', json={'visual_enrichment': False})
    assert response.status_code == 200
    assert response.get_json()['privacy'] == 'local_only'
