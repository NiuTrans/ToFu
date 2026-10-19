"""Inverse SyncTeX: PDF point → nearest owned source line."""
from __future__ import annotations

import gzip
import io

import pytest

from lib.paper.builds.synctex import inverse_stream

pytestmark = pytest.mark.unit

_PT = 72.27 / 72  # bp → sp ratio used by the parser (_SP_TO_BP inverse)


def _sp(bp: float) -> int:
    return int(bp * 65536 * _PT)


def _synctex() -> io.BytesIO:
    raw = ('SyncTeX Version:1\n'
           'Input:1:main.tex\n'
           'Input:2:chapters/intro.tex\n'
           'Input:3:@toolchain/7\n'
           '{1\n'
           # line 5 of main.tex: h=100bp, v(baseline)=200bp, 300x10bp box
           f'(1,5:{_sp(100)},{_sp(190)}:{_sp(300)},{_sp(10)},0\n'
           # line 12 of intro.tex: h=100bp, v=400bp, 300x10bp
           f'(2,12:{_sp(100)},{_sp(390)}:{_sp(300)},{_sp(10)},0\n'
           # toolchain box must never win
           f'(3,1:{_sp(100)},{_sp(200)}:{_sp(300)},{_sp(10)},0\n'
           '}\n'
           '{2\n'
           f'(2,30:{_sp(100)},{_sp(100)}:{_sp(300)},{_sp(10)},0\n'
           '}\n')
    return io.BytesIO(gzip.compress(raw.encode()))


def test_point_inside_box_resolves_its_line():
    result = inverse_stream(_synctex(), build_id='b' * 32, page=1, h=150, v=195)
    assert result['file'] == 'main.tex' and result['line'] == 5


def test_point_between_boxes_picks_nearest_vertically():
    result = inverse_stream(_synctex(), build_id='b' * 32, page=1, h=150, v=350)
    assert result['file'] == 'chapters/intro.tex' and result['line'] == 12


def test_toolchain_labels_are_never_returned():
    result = inverse_stream(_synctex(), build_id='b' * 32, page=1, h=150, v=205)
    assert not result['file'].startswith('@toolchain')


def test_page_miss_is_explicit_and_validates():
    result = inverse_stream(_synctex(), build_id='b' * 32, page=9, h=1, v=1)
    assert result['file'] == '' and result['line'] is None
    assert result['page'] == 9


def test_corrupt_gzip_tail_cannot_return_partial_known_location():
    from lib.paper.builds.synctex import locate_stream
    from lib.paper.builds.errors import BuildError
    encoded=_synctex().getvalue()[:-4]
    for query in (
        lambda stream:locate_stream(stream,build_id='b'*32,file='main.tex',line=5),
        lambda stream:inverse_stream(stream,build_id='b'*32,page=1,h=100,v=200),
    ):
        with pytest.raises(BuildError,match='Cannot decode'):query(io.BytesIO(encoded))
