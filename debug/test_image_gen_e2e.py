#!/usr/bin/env python3
"""End-to-end test: call generate_image() through the lib and verify result."""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.image_gen import generate_image

print('Calling generate_image()...')
t0 = time.time()
result = generate_image(
    prompt='A cute orange cat sitting on a windowsill looking at the sunset',
    aspect_ratio='16:9',
)
elapsed = time.time() - t0

print(f'\nResult in {elapsed:.1f}s:')
print(f"  ok:         {result.get('ok')}")
print(f"  image_b64:  {len(result.get('image_b64', ''))} chars")
print(f"  image_url:  {result.get('image_url', '')[:120]}")
print(f"  mime_type:  {result.get('mime_type', '')}")
print(f"  text:       {result.get('text', '')[:200]}")
print(f"  model:      {result.get('model', '')}")
print(f"  error:      {result.get('error', '')}")

if result.get('ok') and result.get('image_b64'):
    import base64
    img_bytes = base64.b64decode(result['image_b64'])
    out_path = 'debug/test_image_e2e_output.png'
    with open(out_path, 'wb') as f:
        f.write(img_bytes)
    print(f'\n✅ Image saved to {out_path} ({len(img_bytes):,} bytes)')
elif result.get('ok') and result.get('image_url'):
    print(f'\n⚠️ Only URL available (download failed): {result["image_url"]}')
else:
    print(f'\n❌ Generation failed: {result.get("error")}')
    sys.exit(1)
