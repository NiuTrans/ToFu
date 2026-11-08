import { describe, expect, it } from 'vitest';

import { supportedImageComposerOptions } from './composer-constraints';

const offered = { aspects: ['1:1', '16:9', '9:16', '4:3', '3:4'], resolutions: ['1K', '2K'] };

describe('image composer options', () => {
  it('keeps every option when the Model documents no constraint', () => {
    expect(supportedImageComposerOptions(null, offered)).toEqual(offered);
    expect(supportedImageComposerOptions({ sizes: ['1024x1024'], max_outputs: 10 }, offered))
      .toEqual(offered);
  });

  it('narrows to the documented aspect ratios and resolutions', () => {
    expect(supportedImageComposerOptions({
      aspect_ratios: ['1:1', '16:9', '21:9'], resolutions: ['1K'],
    }, offered)).toEqual({ aspects: ['1:1', '16:9'], resolutions: ['1K'] });
  });

  it('never strands the composer when no offered value is documented', () => {
    expect(supportedImageComposerOptions({ aspect_ratios: ['2:3'], resolutions: ['4K'] }, offered))
      .toEqual(offered);
  });

  it('ignores malformed profiles', () => {
    expect(supportedImageComposerOptions({ aspect_ratios: '1:1', resolutions: [7] }, offered))
      .toEqual(offered);
  });
});
