/**
 * Responsibility: own the shared fullscreen-image overlay, its keyboard
 * listener lifecycle, and generated-image download DOM transaction.
 * Entry point: createImageViewerActionsController. Dependencies: an injected
 * document and clock only.
 */

export interface ImageViewerActionsDependencies {
  document: Document;
  now?: () => number;
}

export interface ImageViewerActionsController {
  openImageFullscreen(source: string): boolean;
  downloadGeneratedImage(button: Element | null): boolean;
  closeImageFullscreen(): void;
  destroy(): void;
}

export function createImageViewerActionsController(
  dependencies: ImageViewerActionsDependencies,
): ImageViewerActionsController {
  const browserDocument = dependencies.document;
  const now = dependencies.now ?? Date.now;
  let currentOverlay: HTMLDivElement | null = null;
  let currentImage: HTMLImageElement | null = null;
  let destroyed = false;

  const closeImageFullscreen = (): void => {
    if (currentImage) currentImage.onload = null;
    currentImage = null;
    currentOverlay?.remove();
    currentOverlay = null;
    browserDocument.removeEventListener('keydown', handleKeydown);
  };

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') closeImageFullscreen();
  }

  const openImageFullscreen = (source: string): boolean => {
    if (destroyed) return false;
    closeImageFullscreen();
    browserDocument.querySelectorAll('.imagegen-fullscreen').forEach(
      (element) => element.remove(),
    );

    const overlay = browserDocument.createElement('div');
    overlay.className = 'imagegen-fullscreen';
    const image = browserDocument.createElement('img');
    image.src = source;
    image.onload = () => {
      if (image.naturalHeight <= image.naturalWidth * 1.3) return;
      image.style.maxHeight = 'none';
      overlay.style.overflowY = 'auto';
      overlay.style.alignItems = 'flex-start';
      overlay.style.padding = '20px 0';
    };
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) closeImageFullscreen();
    });
    overlay.appendChild(image);
    browserDocument.body.appendChild(overlay);
    currentOverlay = overlay;
    currentImage = image;
    browserDocument.addEventListener('keydown', handleKeydown);
    return true;
  };

  const downloadGeneratedImage = (button: Element | null): boolean => {
    const card = button?.closest('.imagegen-card')
      ?? button?.closest('.ig-result-card');
    const image = card?.querySelector<HTMLImageElement>('img');
    if (!image) return false;

    const anchor = browserDocument.createElement('a');
    anchor.href = image.src;
    anchor.download = `generated_${now()}.png`;
    browserDocument.body.appendChild(anchor);
    try {
      anchor.click();
    } finally {
      anchor.remove();
    }
    return true;
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    closeImageFullscreen();
  };

  return Object.freeze({
    openImageFullscreen,
    downloadGeneratedImage,
    closeImageFullscreen,
    destroy,
  });
}
