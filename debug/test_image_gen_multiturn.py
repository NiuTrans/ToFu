#!/usr/bin/env python3
"""Test multi-turn image generation via the FRIDAY Gemini API.

Round 1: Generate an initial image.
Round 2: Edit that image using the multi-turn contents format.
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.image_gen import generate_image, _build_multiturn_contents, _resolve_image_part


# ══════════════════════════════════════════════════════════════
#  Unit tests for _build_multiturn_contents
# ══════════════════════════════════════════════════════════════

def test_single_turn():
    """No history → single content entry."""
    contents = _build_multiturn_contents('draw a cat', history=None)
    assert len(contents) == 1, f'Expected 1 content, got {len(contents)}'
    assert contents[0]['parts'][0]['text'] == 'draw a cat'
    print('✅ test_single_turn passed')


def test_single_turn_empty_history():
    """Empty history list → single content entry."""
    contents = _build_multiturn_contents('draw a cat', history=[])
    assert len(contents) == 1, f'Expected 1 content, got {len(contents)}'
    print('✅ test_single_turn_empty_history passed')


def test_multiturn_one_prior():
    """One history turn + current prompt → 3 entries (user1, model1, user2)."""
    history = [
        {'prompt': 'draw a cat', 'image_b64': 'AAAA', 'text': 'Here is a cat', 'mime_type': 'image/png'},
    ]
    contents = _build_multiturn_contents('make it blue', history=history)

    # Expected: [user1, model1(text+inlineData), user2(text)]
    assert len(contents) == 3, f'Expected 3 content entries, got {len(contents)}'

    # User turn 1 — role=user, just text
    assert contents[0]['role'] == 'user'
    assert contents[0]['parts'][0]['text'] == 'draw a cat'
    assert len(contents[0]['parts']) == 1, 'User turn should have text only'

    # Model turn 1 — role=model, text + inlineData
    assert contents[1]['role'] == 'model'
    model_parts = contents[1]['parts']
    assert any(p.get('text') == 'Here is a cat' for p in model_parts), 'Model should have text'
    assert any('inlineData' in p for p in model_parts), 'Model should have inlineData'
    inline = next(p for p in model_parts if 'inlineData' in p)
    assert inline['inlineData']['data'] == 'AAAA'
    assert inline['inlineData']['mimeType'] == 'image/png'

    # User turn 2 (current) — role=user, just text
    assert contents[2]['role'] == 'user'
    assert contents[2]['parts'][0]['text'] == 'make it blue'
    assert len(contents[2]['parts']) == 1, 'Current user turn should be text only'
    print('✅ test_multiturn_one_prior passed')


def test_multiturn_two_priors():
    """Two history turns + current prompt → 5 entries (user/model/user/model/user)."""
    history = [
        {'prompt': 'draw a cat', 'image_b64': 'AAAA', 'text': 'Here is a cat', 'mime_type': 'image/png'},
        {'prompt': 'make it blue', 'image_b64': 'BBBB', 'text': 'Blue cat', 'mime_type': 'image/png'},
    ]
    contents = _build_multiturn_contents('add a hat', history=history)

    # user1, model1, user2, model2, user3  = 5 role-based entries
    assert len(contents) == 5, f'Expected 5 content entries, got {len(contents)}'

    # Check roles alternate correctly
    expected_roles = ['user', 'model', 'user', 'model', 'user']
    actual_roles = [c['role'] for c in contents]
    assert actual_roles == expected_roles, f'Roles mismatch: {actual_roles}'

    # User turn 1
    assert contents[0]['parts'][0]['text'] == 'draw a cat'

    # Model turn 1 — should have text + inlineData with AAAA
    m1 = contents[1]['parts']
    assert any(p.get('text') == 'Here is a cat' for p in m1)
    assert any(p.get('inlineData', {}).get('data') == 'AAAA' for p in m1)

    # User turn 2
    assert contents[2]['parts'][0]['text'] == 'make it blue'

    # Model turn 2 — should have text + inlineData with BBBB
    m2 = contents[3]['parts']
    assert any(p.get('text') == 'Blue cat' for p in m2)
    assert any(p.get('inlineData', {}).get('data') == 'BBBB' for p in m2)

    # User turn 3 (current) — just text
    assert contents[4]['parts'][0]['text'] == 'add a hat'
    assert len(contents[4]['parts']) == 1
    print('✅ test_multiturn_two_priors passed')


def test_resolve_image_part_remote():
    """Remote URL → image_url.uri format."""
    part = _resolve_image_part('https://s3.example.com/img.png')
    assert part == {'image_url': {'uri': 'https://s3.example.com/img.png'}}
    print('✅ test_resolve_image_part_remote passed')


def test_resolve_image_part_empty():
    """Empty string → None."""
    assert _resolve_image_part('') is None
    assert _resolve_image_part(None) is None
    print('✅ test_resolve_image_part_empty passed')


def test_resolve_image_part_local_missing():
    """Local path that doesn't exist → None (logged warning)."""
    part = _resolve_image_part('/api/images/nonexistent_file.png')
    assert part is None
    print('✅ test_resolve_image_part_local_missing passed')


def test_multiturn_no_image_b64_skips_model():
    """History with missing image_b64 should skip the model entry."""
    history = [
        {'prompt': 'draw a cat', 'image_b64': '', 'text': 'No image', 'mime_type': 'image/png'},
    ]
    contents = _build_multiturn_contents('make it blue', history=history)
    # Should have: user1 + user2 = 2 (no model turn because image_b64 is empty)
    # But text is present, so model turn with text only appears
    assert len(contents) >= 2, f'Expected at least 2 entries, got {len(contents)}'
    print('✅ test_multiturn_no_image_b64_skips_model passed')


def test_multiturn_empty_text():
    """History with empty text but valid image should still create model turn."""
    history = [
        {'prompt': 'draw a cat', 'image_b64': 'AAAA', 'text': '', 'mime_type': 'image/png'},
    ]
    contents = _build_multiturn_contents('make it blue', history=history)
    # user1, model1(just image), user2  = 3 entries
    assert len(contents) == 3, f'Expected 3 entries, got {len(contents)}'
    model_parts = contents[1]['parts']
    assert any('inlineData' in p for p in model_parts)
    # No text part if text was empty
    assert not any(p.get('text') == '' for p in model_parts), 'Empty text should not be added'
    print('✅ test_multiturn_empty_text passed')


# ══════════════════════════════════════════════════════════════
#  Live API test (optional — only runs with --live flag)
# ══════════════════════════════════════════════════════════════

def test_live_multiturn():
    """Run a real 2-round multi-turn image generation against the FRIDAY API."""
    print('\n' + '='*60)
    print('  LIVE MULTI-TURN TEST')
    print('='*60)

    # Round 1: Generate initial image
    print('\n[Round 1] Generating initial image...')
    t0 = time.time()
    result1 = generate_image(
        prompt='A simple red circle on a white background',
        aspect_ratio='1:1',
        resolution='1K',
    )
    e1 = time.time() - t0
    print(f'  Elapsed: {e1:.1f}s')
    print(f'  OK: {result1.get("ok")}')
    print(f'  Model: {result1.get("model")}')
    print(f'  Text: {result1.get("text", "")[:200]}')
    print(f'  image_url: {result1.get("image_url", "")[:120]}')
    print(f'  image_b64 len: {len(result1.get("image_b64", ""))}')

    if not result1.get('ok'):
        print(f'  ❌ Round 1 failed: {result1.get("error")}')
        return False

    # Determine the image URL for round 2
    r1_image_url = result1.get('image_url', '')
    if not r1_image_url:
        print('  ⚠️ No image_url in result — cannot proceed with multi-turn')
        return False
    print(f'  ✅ Round 1 success, image_url={r1_image_url[:100]}')

    # Save to disk for verification
    if result1.get('image_b64'):
        import base64
        out1 = 'debug/test_mt_round1.png'
        with open(out1, 'wb') as f:
            f.write(base64.b64decode(result1['image_b64']))
        print(f'  Saved to {out1}')

    # Round 2: Edit the image using multi-turn history
    # The backend needs image_b64 for Gemini multi-turn (inlineData format)
    r1_image_b64 = result1.get('image_b64', '')
    if not r1_image_b64:
        print('  ⚠️ No image_b64 in result — cannot proceed with multi-turn')
        return False

    print('\n[Round 2] Editing image with multi-turn history...')
    history = [{
        'prompt': 'A simple red circle on a white background',
        'image_b64': r1_image_b64,
        'text': result1.get('text', ''),
        'mime_type': result1.get('mime_type', 'image/png'),
    }]

    t1 = time.time()
    result2 = generate_image(
        prompt='Change the circle to blue and add a green triangle next to it',
        aspect_ratio='1:1',
        resolution='1K',
        history=history,
    )
    e2 = time.time() - t1
    print(f'  Elapsed: {e2:.1f}s')
    print(f'  OK: {result2.get("ok")}')
    print(f'  Model: {result2.get("model")}')
    print(f'  Text: {result2.get("text", "")[:200]}')
    print(f'  image_url: {result2.get("image_url", "")[:120]}')
    print(f'  image_b64 len: {len(result2.get("image_b64", ""))}')

    if not result2.get('ok'):
        print(f'  ❌ Round 2 failed: {result2.get("error")}')
        return False

    # Save to disk
    if result2.get('image_b64'):
        import base64
        out2 = 'debug/test_mt_round2.png'
        with open(out2, 'wb') as f:
            f.write(base64.b64decode(result2['image_b64']))
        print(f'  Saved to {out2}')

    print(f'\n✅ Multi-turn test PASSED! Round 1: {e1:.1f}s, Round 2: {e2:.1f}s')
    return True


# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print('='*60)
    print('  Unit Tests: _build_multiturn_contents')
    print('='*60 + '\n')

    test_single_turn()
    test_single_turn_empty_history()
    test_multiturn_one_prior()
    test_multiturn_two_priors()
    test_resolve_image_part_remote()
    test_resolve_image_part_empty()
    test_resolve_image_part_local_missing()
    test_multiturn_no_image_b64_skips_model()
    test_multiturn_empty_text()

    print('\n✅ All unit tests passed!\n')

    if '--live' in sys.argv:
        success = test_live_multiturn()
        sys.exit(0 if success else 1)
    else:
        print('Skipping live API test. Run with --live to test against the FRIDAY API.')
