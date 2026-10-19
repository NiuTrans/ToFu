"""Location must use the same build, exact source label and PDF-point units."""
from __future__ import annotations

import gzip

import pytest

from lib.paper.builds.synctex import locate, normalize

pytestmark = pytest.mark.unit


def test_normalized_labels_survive_staging_removal_and_do_not_match_packages(tmp_path):
    root = tmp_path / 'snapshot'
    root.mkdir()
    (root / 'main.tex').write_text('source')
    compressed = tmp_path / 'raw.gz'
    raw = ('SyncTeX Version:1\n'
           f'Input:1:{root}/main.tex\n'
           'Input:2:/system/tex/main.tex\n'
           '{1\n(2,5:6553600,13107200:655360,655360,0\n}\n'
           '{2\n(1,5:6553600,13107200:655360,655360,0\n}\n')
    compressed.write_bytes(gzip.compress(raw.encode()))
    output = tmp_path / 'document.synctex.gz'
    normalize(compressed, output, source_root=root, source_files=({'path': 'main.tex'},))
    (root / 'main.tex').unlink()
    root.rmdir()
    result = locate(output, build_id='a' * 32, file='main.tex', line=5)
    assert result['build_id'] == 'a' * 32
    assert result['page'] == 2
    assert result['rects'][0]['h'] == pytest.approx(100 * 72 / 72.27)
    assert result['rects'][0]['v'] == pytest.approx(190 * 72 / 72.27)
    assert str(root) not in gzip.decompress(output.read_bytes()).decode()
    assert '/system/' not in gzip.decompress(output.read_bytes()).decode()


def test_missing_mapping_is_explicit_and_does_not_invent_a_page(tmp_path):
    result = locate(tmp_path / 'missing', build_id='b' * 32, file='intro.tex', line=3)
    assert result['page'] is None and result['rects'] == []


_TEX = '\\section{Intro}\nHello.\n\\section{Method}\n  \\subsection{A}\n'


def test_needle_to_line_exact_then_whitespace_tolerant():
    from lib.paper.builds.location import needle_to_line
    assert needle_to_line(_TEX, 'Hello.') == 2
    assert needle_to_line(_TEX, '\\section{Method}\n  \\subsection{A}') == 3
    # Block drifted but the first line still identifies the edit position.
    assert needle_to_line(_TEX, '\\section{Method}\nparagraph the file no longer has') == 3
    # The agent's new_string lost the leading indent the file carries.
    assert needle_to_line(_TEX, '\\subsection{A}\nnew') == 4


def test_needle_to_line_no_match_is_explicit():
    from lib.paper.builds.location import needle_to_line
    assert needle_to_line(_TEX, 'never appears') is None
    assert needle_to_line(_TEX, '') is None
    assert needle_to_line(_TEX, '   \n  ') is None


def test_complete_mapping_after_old_decoded_quota_survives_source_removal(tmp_path):
    import io
    import hashlib
    from lib.paper.builds.synctex import inverse_stream
    root=tmp_path/'snapshot';root.mkdir();(root/'main.tex').write_text('source')
    source=tmp_path/'large.gz';output=tmp_path/'normalized.gz'
    head=f'SyncTeX Version:1\nInput:1:{root}/main.tex\n'.encode()
    box=b'(1,5:6553600,13107200:655360,655360,0\n'
    padding=b'{1\n'+box*(17*1024*1024//len(box)+1)+b'}\n'
    tail=b'{1001\n(1,999:6553600,13107200:655360,655360,0\n}\n'
    raw=head+padding+tail;source.write_bytes(gzip.compress(raw))
    before=hashlib.sha256(source.read_bytes()).digest()
    normalize(source,output,source_root=root,source_files=({'path':'main.tex'},))
    expected=b'SyncTeX Version:1\nInput:1:main.tex\n'+padding+tail
    assert gzip.decompress(output.read_bytes())==expected
    assert hashlib.sha256(source.read_bytes()).digest()==before
    (root/'main.tex').unlink();root.rmdir()
    result=locate(output,build_id='a'*32,file='main.tex',line=999)
    assert result['page']==1001 and result['rects']
    with output.open('rb') as stream:
        inverse=inverse_stream(stream,build_id='a'*32,page=1001,h=100*72/72.27,v=195*72/72.27)
    assert inverse['file']=='main.tex' and inverse['line']==999
    assert not list(tmp_path.glob('.synctex-normalize-*'))


@pytest.mark.parametrize('fault',['truncated','crc','bad_input','replace'])
def test_normalization_failure_preserves_previous_complete_mapping(tmp_path,monkeypatch,fault):
    from lib.paper.builds import synctex
    from lib.paper.builds.errors import BuildError
    root=tmp_path/'root';root.mkdir();(root/'main.tex').write_text('source')
    source=tmp_path/'raw.gz';destination=tmp_path/'good.gz';previous=gzip.compress(b'previous complete source')
    destination.write_bytes(previous)
    encoded=gzip.compress(b'Input:1:main.tex\n{1\n}\n')
    if fault=='truncated':encoded=encoded[:-4]
    elif fault=='crc':encoded=encoded[:-8]+bytes([encoded[-8]^1])+encoded[-7:]
    elif fault=='bad_input':encoded=gzip.compress(b'Input:broken\n')
    else:monkeypatch.setattr(synctex.os,'replace',lambda *a:(_ for _ in ()).throw(OSError('injected replace failure')))
    source.write_bytes(encoded)
    with pytest.raises(BuildError,match='Cannot decode'):normalize(source,destination,source_root=root,source_files=({'path':'main.tex'},))
    assert destination.read_bytes()==previous
    assert not list(tmp_path.glob('.synctex-normalize-*'))


def test_normalized_artifact_bytes_are_stable_for_identical_source(tmp_path):
    root=tmp_path/'root';root.mkdir();(root/'main.tex').write_text('source')
    source=tmp_path/'raw.gz';source.write_bytes(gzip.compress(b'Input:1:main.tex\n{1\n}\n'))
    first=tmp_path/'first.gz';second=tmp_path/'second.gz'
    for output in (first,second):normalize(source,output,source_root=root,source_files=({'path':'main.tex'},))
    assert first.read_bytes()==second.read_bytes()
