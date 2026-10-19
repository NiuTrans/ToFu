"""Real large PDF, Office and image sources reach native readers without quotas."""
import base64
import hashlib
import io
import zipfile

import pytest

from tests.paper.test_pdf_source_demand import large_pdf as large_pdf

pytestmark=pytest.mark.unit


def test_large_pdf_local_reader_extracts_real_text(large_pdf,tmp_path):
    from lib.file_reader import read_local_file
    path=tmp_path/'large.pdf';path.write_bytes(large_pdf)
    result=read_local_file(str(path))
    assert isinstance(result,str) and result.startswith('PDF:')
    assert 'Full PDF source marker' in result
    assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(large_pdf).digest()


def test_large_office_source_retains_text_past_both_retired_limits(tmp_path):
    from lib.file_reader import read_local_file
    path=tmp_path/'large.docx'
    prefix=b'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    suffix=b'<w:sectPr/></w:body></w:document>'
    block=b'q'*(1024*1024);marker=b'COMPLETE_OFFICE_SOURCE_TAIL';digest=hashlib.sha256()
    content_types=('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="bin" ContentType="application/octet-stream"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    relationships=('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('[Content_Types].xml',content_types)
        archive.writestr('_rels/.rels',relationships)
        xml=zipfile.ZipInfo('word/document.xml');xml.compress_type=zipfile.ZIP_DEFLATED
        with archive.open(xml,'w') as output:
            output.write(prefix)
            for _ in range(51):
                output.write(b'<w:p><w:r><w:t>');output.write(block);output.write(b'</w:t></w:r></w:p>')
                digest.update(block);digest.update(b'\n')
            output.write(b'<w:p><w:r><w:t>'+marker+b'</w:t></w:r></w:p>');digest.update(marker)
            output.write(suffix)
        unused=zipfile.ZipInfo('word/media/unused.bin');unused.compress_type=zipfile.ZIP_STORED
        with archive.open(unused,'w') as output:
            for _ in range(51):output.write(block)
    assert path.stat().st_size>50*1024*1024
    original_digest=hashlib.sha256(path.read_bytes()).digest()
    result=read_local_file(str(path))
    assert result.startswith('Document:'),result[:200]
    body=result.split('\n\n',1)[1]
    assert len(body)>50*1024*1024
    assert body.endswith(marker.decode())
    assert hashlib.sha256(body.encode()).digest()==digest.digest()
    assert hashlib.sha256(path.read_bytes()).digest()==original_digest


def test_large_original_image_supports_read_and_both_crop_entry_points(tmp_path):
    from PIL import Image,ImageDraw
    from lib.file_reader import read_local_file,inspect_image_file,inspect_image_bytes
    path=tmp_path/'large.bmp'
    with Image.new('RGB',(4096,3072),(0,0,255)) as source:
        ImageDraw.Draw(source).rectangle((3500,2600,3600,2700),fill=(255,0,0))
        source.save(path,format='BMP')
    raw=path.read_bytes();assert len(raw)>20*1024*1024
    expected=hashlib.sha256(raw).digest()
    read=read_local_file(str(path))
    assert read['__screenshot__'] and read['originalSize']==len(raw)
    assert read['compressionApplied']
    for result in (inspect_image_file(str(path),crop=[3500,2600,3600,2700]),
                   inspect_image_bytes(raw,source_name='large.bmp',crop=[3500,2600,3600,2700])):
        assert result['__screenshot__']
        preview=base64.b64decode(result['dataUrl'].split(',',1)[1])
        with Image.open(io.BytesIO(preview)) as image:
            red,green,blue=image.convert('RGB').getpixel((image.width//2,image.height//2))
            assert red>200 and green<80 and blue<80
    assert hashlib.sha256(path.read_bytes()).digest()==expected


def test_failed_office_parser_is_not_successful_placeholder_text(tmp_path):
    from lib.file_reader import read_local_file
    path=tmp_path/'invalid.docx';path.write_bytes(b'PK\x03\x04invalid package')
    result=read_local_file(str(path))
    assert result.startswith('Error:'),result


def test_unavailable_office_parser_is_not_successful_placeholder_text(tmp_path,monkeypatch):
    import sys
    from lib.file_reader import read_local_file
    path=tmp_path/'unavailable.docx';path.write_bytes(b'placeholder source')
    monkeypatch.setitem(sys.modules,'docx',None)
    result=read_local_file(str(path))
    assert result.startswith('Error:'),result
