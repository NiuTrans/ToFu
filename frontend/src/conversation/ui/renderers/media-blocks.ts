/**
 * Image generation, artifact and attachment DOM projections. Resolves media
 * through injected ports and rejects unsupported URL schemes.
 * Entry points: renderImageGenerationBlock, renderArtifactsBlock,
 * renderAttachmentsBlock.
 * Dependencies: conversation-view-model, ports, icons, localized-copy.
 */
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import {
  documentAttachmentGlyph,
  lineSvgIcon,
} from './icons';
import {
  textFor,
  formatDuration,
  displayJson,
} from './localized-copy';

function safeMediaUrl(
  raw: string | undefined,
  ports: ClassicConversationRendererPorts,
): string {
  if (!raw) return '';
  const resolved = (ports.resolveMediaUrl?.(raw) ?? raw).trim();
  if (/^(?:https?:|blob:)/i.test(resolved)) return resolved;
  if (/^data:image\/(?:avif|gif|jpe?g|png|webp);base64,/i.test(resolved)) {
    return resolved;
  }
  if (/^(?:\/|\.\.?\/)/.test(resolved)) return resolved;
  return '';
}

function compactFileSize(bytes: number | undefined): string {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function renderImageGenerationBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'image-generation' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const generation = block.value;
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--image-generation message-body';
  const wrapper = document.createElement('div');
  wrapper.className = generation.mode === 'batch'
    ? 'ig-batch-wrapper' : 'ig-result-wrapper';
  if (generation.mode === 'batch' || generation.results.length !== 1) {
    const banner = document.createElement('div');
    banner.className = 'ig-batch-banner';
    const successes = generation.results.filter((result) => result.ok).length;
    banner.textContent = generation.status === 'running'
      ? textFor(ports, 'chat.igGenerating', 'Generating…')
      : `${generation.results.length} results · ${successes} succeeded`;
    wrapper.appendChild(banner);
  }
  const grid = document.createElement('div');
  grid.className = generation.mode === 'batch'
    ? `ig-batch-grid ig-cols-${Math.min(Math.max(generation.results.length, 1), 2)}`
    : 'ig-result-card-grid';
  generation.results.forEach((result, index) => {
    const slot = document.createElement('div');
    slot.className = 'ig-batch-slot';
    slot.dataset.slotIdx = String(index);
    const source = safeMediaUrl(result.imageUrl, ports);
    if (result.ok && source) {
      const card = document.createElement('div');
      card.className = 'ig-result-card';
      const preview = document.createElement('button');
      preview.type = 'button';
      preview.className = 'ig-result-preview';
      preview.dataset.conversationAction = 'preview-generated-image';
      preview.dataset.operation = String(index);
      preview.setAttribute('aria-label', result.prompt || 'Preview generated image');
      const image = document.createElement('img');
      image.src = source;
      image.alt = result.prompt || 'Generated image';
      image.loading = 'lazy';
      preview.appendChild(image);
      const footer = document.createElement('div');
      footer.className = 'ig-result-footer';
      const prompt = document.createElement('span');
      prompt.className = 'ig-result-prompt';
      prompt.textContent = result.prompt || result.model;
      prompt.title = result.prompt || '';
      const metadata = document.createElement('div');
      metadata.className = 'ig-result-meta';
      for (const value of [
        result.model,
        result.providerId ? `@${result.providerId}` : '',
        result.aspectRatio,
        compactFileSize(result.fileSize),
        result.elapsedSeconds == null ? '' : `${result.elapsedSeconds.toFixed(1)}s`,
      ].filter(Boolean)) {
        const pill = document.createElement('span');
        pill.className = 'ig-meta-pill';
        pill.textContent = String(value);
        metadata.appendChild(pill);
      }
      const actions = document.createElement('div');
      actions.className = 'ig-result-actions';
      const download = document.createElement('a');
      download.href = source;
      download.download = `generated-image-${index + 1}`;
      download.rel = 'noopener';
      download.title = 'Download';
      download.setAttribute('aria-label', 'Download');
      download.appendChild(lineSvgIcon(document, [
        'M8 2.5v7.25', 'm5.25 7 2.75 2.75L10.75 7', 'M3 13h10',
      ]));
      actions.appendChild(download);
      footer.append(prompt, metadata, actions);
      card.append(preview, footer);
      slot.appendChild(card);
    } else if (generation.status === 'running' || result.error === 'pending') {
      slot.classList.add('ig-generating', 'ig-batch-loading');
      const spinner = document.createElement('div');
      spinner.className = 'ig-gen-spinner';
      const title = document.createElement('div');
      title.className = 'ig-gen-title';
      title.textContent = result.model;
      const status = document.createElement('div');
      status.className = 'ig-gen-subtitle';
      status.textContent = textFor(ports, 'chat.igGenerating', 'Generating…');
      slot.append(spinner, title, status);
    } else {
      const error = document.createElement('div');
      error.className = `ig-batch-error ig-error-${result.errorType || 'generic'}`;
      const title = document.createElement('div');
      title.className = 'ig-error-title';
      title.textContent = result.model || 'Image generation';
      const detail = document.createElement('div');
      detail.className = 'ig-error-text';
      detail.textContent = result.error || 'Generation failed';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'ig-slot-retry-btn';
      retry.dataset.conversationAction = 'retry-image-generation';
      retry.dataset.operation = String(index);
      retry.textContent = textFor(ports, 'chat.retry', 'Retry');
      error.append(title, detail, retry);
      slot.appendChild(error);
    }
    grid.appendChild(slot);
  });
  if (!generation.results.length && generation.error != null) {
    const error = document.createElement('div');
    error.className = 'ig-batch-error ig-error-generic';
    error.textContent = typeof generation.error === 'string'
      ? generation.error : displayJson(generation.error);
    grid.appendChild(error);
  }
  wrapper.appendChild(grid);
  if (generation.status === 'running') {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'ig-gen-cancel';
    cancel.dataset.conversationAction = 'cancel-image-generation';
    cancel.textContent = textFor(ports, 'chat.cancelGeneration', 'Cancel');
    wrapper.appendChild(cancel);
  }
  node.replaceChildren(wrapper);
}

function formatArtifactBytes(value: number | undefined): string {
  if (!Number.isFinite(value) || Number(value) <= 0) return '';
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function renderArtifactsBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'artifacts' }>,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--artifacts artifact-chip-row';
  const byPath = new Map<string, typeof block.artifacts[number]>();
  const withoutPath: typeof block.artifacts[number][] = [];
  for (const artifact of block.artifacts) {
    if (!artifact.sourcePath) {
      withoutPath.push(artifact);
      continue;
    }
    const previous = byPath.get(artifact.sourcePath);
    if (!previous || (artifact.version ?? 0) > (previous.version ?? 0)
        || ((artifact.version ?? 0) === (previous.version ?? 0)
          && (artifact.createdAt ?? 0) > (previous.createdAt ?? 0))) {
      byPath.set(artifact.sourcePath, artifact);
    }
  }
  const visible = [...withoutPath, ...byPath.values()];
  const chips = visible.map((artifact) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `artifact-chip artifact-chip-${artifact.format || 'doc'}`;
    button.dataset.conversationAction = 'open-artifact';
    button.dataset.operation = artifact.id;
    button.dataset.artifactId = artifact.id;
    button.title = `Open ${artifact.title}`;
    const main = document.createElement('span');
    main.className = 'artifact-chip-main';
    const title = document.createElement('span');
    title.className = 'artifact-chip-title';
    title.textContent = artifact.title;
    const meta = document.createElement('span');
    meta.className = 'artifact-chip-meta';
    const format = document.createElement('span');
    format.className = 'artifact-chip-fmt';
    format.textContent = artifact.format.toUpperCase();
    meta.appendChild(format);
    const size = formatArtifactBytes(artifact.sizeBytes);
    if (size) {
      const sizeNode = document.createElement('span');
      sizeNode.className = 'artifact-chip-size';
      sizeNode.textContent = size;
      meta.appendChild(sizeNode);
    }
    main.append(title, meta);
    button.appendChild(main);
    return button;
  });
  node.replaceChildren(...chips);
}

function appendBadgeText(
  parent: HTMLElement,
  name: string,
  meta: string,
  iconText = '',
): void {
  if (iconText) {
    const icon = parent.ownerDocument.createElement('span');
    icon.className = 'reply-quote-badge-icon';
    icon.textContent = iconText;
    parent.appendChild(icon);
  }
  const info = parent.ownerDocument.createElement('span');
  info.className = 'reply-quote-badge-info';
  const title = parent.ownerDocument.createElement('span');
  title.className = 'reply-quote-badge-name';
  title.textContent = name;
  const metadata = parent.ownerDocument.createElement('span');
  metadata.className = 'reply-quote-badge-meta';
  metadata.textContent = meta;
  info.append(title, metadata);
  parent.appendChild(info);
}

export function renderAttachmentsBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'attachments' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--attachments message-body';
  const document = node.ownerDocument;
  const children: HTMLElement[] = [];
  if (block.images.length) {
    const grid = document.createElement('div');
    grid.className = 'msg-image-grid';
    const sourceLabels: Readonly<Record<string, string>> = {
      clip_render: 'CLIP', vector_clip: 'VEC', page_render: 'SCAN',
      embedded: 'RAW', pixmap_fallback: 'PIX', pymupdf4llm: 'FIG',
      figure_page_render: 'FIG',
    };
    block.images.forEach((item, index) => {
      const source = safeMediaUrl(item.preview, ports);
      const interactive = Boolean(source && !source.endsWith('...'));
      const thumb = document.createElement(interactive ? 'button' : 'div');
      thumb.className = `msg-img-thumb conversation-attachment-button${
        item.pdfPage ? ' pdf-page' : ''}${interactive ? '' : ' placeholder'}`;
      if (interactive) {
        (thumb as HTMLButtonElement).type = 'button';
        thumb.dataset.conversationAction = 'preview-image';
        thumb.dataset.operation = String(index);
        thumb.setAttribute('aria-label', item.caption || 'Preview image');
      }
      if (item.caption) thumb.title = item.caption;
      if (interactive) {
        const image = document.createElement('img');
        image.src = source;
        image.alt = item.caption || 'Image attachment';
        image.width = 80;
        image.height = 80;
        image.loading = 'lazy';
        thumb.appendChild(image);
      } else {
        const placeholder = document.createElement('span');
        placeholder.className = 'msg-img-placeholder-icon';
        placeholder.textContent = 'IMG';
        thumb.appendChild(placeholder);
      }
      const sourceLabel = sourceLabels[item.pdfImageSource ?? '']
        || (item.pdfPage ? 'PDF' : '');
      if (sourceLabel) {
        const badge = document.createElement('span');
        badge.className = 'msg-img-badge';
        badge.textContent = sourceLabel;
        thumb.appendChild(badge);
      }
      const size = document.createElement('span');
      size.className = 'msg-img-size';
      size.textContent = item.pdfPage
        ? `P${item.pdfPage}/${item.pdfTotal ?? '?'} · ${item.sizeKB ?? '?'}KB`
        : `${item.sizeKB ?? '?'}KB`;
      thumb.appendChild(size);
      grid.appendChild(thumb);
    });
    children.push(grid);
  }
  const videoItems = [
    ...block.videos.map((item, index) => ({ item, legacyIndex: index })),
    ...block.mediaAttachments.filter((item) => item.kind === 'video')
      .map((item) => ({ item, legacyIndex: null })),
  ];
  if (videoItems.length) {
    const list = document.createElement('div');
    list.className = 'msg-video-list';
    videoItems.forEach(({ item, legacyIndex }) => {
      const unified = legacyIndex === null
        ? item as typeof block.mediaAttachments[number] : null;
      const legacy = legacyIndex === null
        ? null : item as typeof block.videos[number];
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'msg-video-card conversation-attachment-button';
      const videoUrl = safeMediaUrl(
        unified?.sourceUrl ?? legacy?.video_url, ports);
      if (videoUrl) {
        card.dataset.conversationAction = unified ? 'open-media' : 'open-video';
        card.dataset.operation = unified
          ? unified.attachmentId : String(legacyIndex);
      } else {
        card.disabled = true;
      }
      const itemName = unified?.name ?? legacy?.name ?? 'video';
      card.title = itemName;
      const thumb = document.createElement('span');
      thumb.className = 'msg-video-thumb';
      const poster = safeMediaUrl(
        unified?.previewUrl ?? legacy?.poster, ports);
      if (poster) {
        const image = document.createElement('img');
        image.src = poster;
        image.alt = `${itemName} poster`;
        image.width = 72;
        image.height = 44;
        image.loading = 'lazy';
        thumb.appendChild(image);
      }
      const play = document.createElement('span');
      play.className = 'msg-video-play';
      play.appendChild(lineSvgIcon(document, [
        'M5.5 3.75 12 8l-6.5 4.25v-8.5Z',
      ]));
      thumb.appendChild(play);
      const info = document.createElement('span');
      info.className = 'msg-video-info';
      const name = document.createElement('span');
      name.className = 'msg-video-name';
      name.textContent = itemName;
      const metadata = document.createElement('span');
      metadata.className = 'msg-video-meta';
      const frameCount = unified
        ? (unified.frameCount ?? 0)
        : (legacy?.frame_count ?? legacy?.frames?.length ?? 0);
      metadata.textContent = [
        (unified?.durationSeconds ?? legacy?.duration_s)
          ? formatDuration(unified?.durationSeconds ?? legacy?.duration_s ?? 0)
          : '',
        `${frameCount} ${textFor(ports, 'upload.videoFrames', 'frames')}`,
        unified
          ? unified.status
          : (legacy?.transcript
            ? textFor(ports, 'upload.videoTranscript', 'transcript') : ''),
      ].filter(Boolean).join(' · ');
      info.append(name, metadata);
      card.append(thumb, info);
      list.appendChild(card);
    });
    children.push(list);
  }
  const documentItems = [
    ...block.pdfTexts.map((item, index) => ({ item, legacyIndex: index })),
    ...block.mediaAttachments.filter((item) => item.kind === 'document')
      .map((item) => ({ item, legacyIndex: null })),
  ];
  if (documentItems.length) {
    const documents = document.createElement('div');
    documents.className = 'pdf-attachments-indicator';
    documentItems.forEach(({ item, legacyIndex }) => {
      const unified = legacyIndex === null
        ? item as typeof block.mediaAttachments[number] : null;
      const legacy = legacyIndex === null
        ? null : item as typeof block.pdfTexts[number];
      const badge = document.createElement('button');
      badge.type = 'button';
      badge.className = 'pdf-attach-badge conversation-attachment-button';
      badge.dataset.conversationAction = unified ? 'open-media' : 'preview-document';
      badge.dataset.operation = unified
        ? unified.attachmentId : String(legacyIndex);
      badge.title = unified?.name ?? legacy?.name ?? 'Document';
      const icon = document.createElement('span');
      icon.className = 'pdf-attach-icon';
      icon.textContent = documentAttachmentGlyph(unified?.name ?? legacy?.name ?? '');
      const info = document.createElement('span');
      info.className = 'pdf-attach-info';
      const name = document.createElement('span');
      name.className = 'pdf-attach-name';
      const fullName = unified?.name ?? legacy?.name ?? 'Document';
      name.textContent = fullName.length > 25 ? `${fullName.slice(0, 23)}…` : fullName;
      const metadata = document.createElement('span');
      metadata.className = 'pdf-attach-meta';
      const length = unified
        ? (unified.textChars ?? 0)
        : (legacy?.textLength ?? legacy?.text?.length ?? 0);
      const lengthText = length >= 1024 ? `${(length / 1024).toFixed(1)}KB`
        : `${length} chars`;
      metadata.textContent = [
        `${unified?.pages ?? legacy?.pages ?? '?'} pages`, lengthText,
        unified ? unified.status : (legacy?.isScanned ? 'scanned' : ''),
        (unified?.method ?? legacy?.method) === 'vlm' ? 'VLM' : '',
      ].filter(Boolean).join(' · ');
      info.append(name, metadata);
      badge.append(icon, info);
      documents.appendChild(badge);
    });
    children.push(documents);
  }
  for (const quote of block.replyQuotes) {
    const badge = document.createElement('div');
    badge.className = 'reply-quote-badge';
    const preview = quote.replace(/\s+/g, ' ').slice(0, 80);
    badge.title = quote.slice(0, 300);
    const lines = quote.split('\n').length;
    appendBadgeText(badge, `${preview}${quote.length > 80 ? '…' : ''}`,
      `${quote.length} chars · ${lines} line${lines === 1 ? '' : 's'}`);
    children.push(badge);
  }
  for (const reference of block.conversationReferences) {
    const badge = document.createElement('div');
    badge.className = 'reply-quote-badge conv-ref-badge';
    const title = reference.title || reference.id || 'Conversation';
    badge.title = title;
    appendBadgeText(badge, title,
      textFor(ports, 'chat.convRefMeta', 'Conversation reference'), '@');
    children.push(badge);
  }
  node.replaceChildren(...children);
}
