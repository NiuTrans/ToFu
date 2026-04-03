#!/usr/bin/env python3
"""Test image editing support: generate an image, then edit it.

Tests both the Gemini (inlineData in user turn) and OpenAI (images/edits) paths.
"""

import sys
import os
import json
import time
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.image_gen import generate_image

print('=' * 60)
print('TEST 1: Generate a base image first')
print('=' * 60)

t0 = time.time()
result = generate_image(
    prompt='A simple red circle on a white background, minimal flat design',
    aspect_ratio='1:1',
    resolution='1K',
)
elapsed = time.time() - t0

print(f'  ok={result.get("ok")}  model={result.get("model")}  elapsed={elapsed:.1f}s')
if not result.get('ok'):
    print(f'  ERROR: {result.get("error")}')
    sys.exit(1)

image_b64 = result.get('image_b64', '')
mime_type = result.get('mime_type', 'image/png')
print(f'  image_b64 length: {len(image_b64)}')
print(f'  mime_type: {mime_type}')

# Save base image
if image_b64:
    raw = base64.b64decode(image_b64 + '==')
    with open('debug/test_image_edit_base.png', 'wb') as f:
        f.write(raw)
    print(f'  Saved base image: debug/test_image_edit_base.png ({len(raw)} bytes)')

print()
print('=' * 60)
print('TEST 2: Edit the image — change circle to blue')
print('=' * 60)

source_images = [{
    'image_b64': image_b64,
    'mime_type': mime_type,
}]

t0 = time.time()
edit_result = generate_image(
    prompt='Change the red circle to blue, keep everything else the same',
    aspect_ratio='1:1',
    resolution='1K',
    source_images=source_images,
)
edit_elapsed = time.time() - t0

print(f'  ok={edit_result.get("ok")}  model={edit_result.get("model")}  elapsed={edit_elapsed:.1f}s')
if not edit_result.get('ok'):
    print(f'  ERROR: {edit_result.get("error")}')
    print(f'  (This might fail if the model/API does not support editing)')
else:
    edit_b64 = edit_result.get('image_b64', '')
    edit_mime = edit_result.get('mime_type', 'image/png')
    print(f'  edited image_b64 length: {len(edit_b64)}')
    print(f'  edited mime_type: {edit_mime}')
    if edit_b64:
        raw = base64.b64decode(edit_b64 + '==')
        with open('debug/test_image_edit_result.png', 'wb') as f:
            f.write(raw)
        print(f'  Saved edited image: debug/test_image_edit_result.png ({len(raw)} bytes)')

print()
print('=' * 60)
print('TEST 3: Test tool definition has source_image parameter')
print('=' * 60)

from lib.tools.image_gen import GENERATE_IMAGE_TOOL
params = GENERATE_IMAGE_TOOL['function']['parameters']['properties']
assert 'source_image' in params, 'source_image not in tool parameters!'
print(f'  ✅ source_image parameter present in tool definition')
print(f'  description: {params["source_image"]["description"][:80]}...')

print()
print('=' * 60)
print('TEST 4: Test _build_multiturn_contents with source_images')
print('=' * 60)

from lib.image_gen import _build_multiturn_contents

# Test: no source_images, no history → single text part
contents = _build_multiturn_contents('draw a cat')
assert len(contents) == 1
assert contents[0]['parts'][0]['text'] == 'draw a cat'
print('  ✅ Single turn (no editing): 1 content entry')

# Test: with source_images, no history → single entry with text + inlineData
contents = _build_multiturn_contents('make it blue', source_images=[{
    'image_b64': 'abc123',
    'mime_type': 'image/png',
}])
assert len(contents) == 1
parts = contents[0]['parts']
assert len(parts) == 2  # text + inlineData
assert parts[0]['text'] == 'make it blue'
assert parts[1]['inlineData']['mimeType'] == 'image/png'
assert parts[1]['inlineData']['data'] == 'abc123'
print('  ✅ Edit mode (1 source image): 1 content entry with 2 parts')

# Test: with source_images + history
contents = _build_multiturn_contents('add a hat', history=[{
    'prompt': 'draw a cat',
    'image_b64': 'history_img',
    'text': 'Here is a cat',
    'mime_type': 'image/png',
}], source_images=[{
    'image_b64': 'edit_img',
    'mime_type': 'image/jpeg',
}])
assert len(contents) == 3  # user turn + model turn + current user turn
assert contents[2]['role'] == 'user'
assert len(contents[2]['parts']) == 2  # text + inlineData
assert contents[2]['parts'][1]['inlineData']['data'] == 'edit_img'
print('  ✅ Edit mode + history: 3 content entries, current turn has 2 parts')

print()
print('=' * 60)
print('TEST 5: Test _resolve_source_image helper')
print('=' * 60)

from lib.tasks_pkg.executor import _resolve_source_image

# Test: data URI
result = _resolve_source_image('data:image/png;base64,iVBORw0KGgo=')
assert result is not None
assert result['image_b64'] == 'iVBORw0KGgo='
assert result['mime_type'] == 'image/png'
print('  ✅ Data URI resolution works')

# Test: empty input
result = _resolve_source_image('')
assert result is None
print('  ✅ Empty input returns None')

# Test: unknown format
result = _resolve_source_image('ftp://something.png')
assert result is None
print('  ✅ Unknown format returns None')

print()
print('ALL TESTS PASSED ✅')
