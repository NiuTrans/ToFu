/**
 * Pure presentation policy for image-reading, inspection, generation, and
 * editing tool rounds.
 *
 * Responsibility: render bounded, localized image-tool HTML from projected
 * round metadata. Entry point: `createToolImagePresentation`. Dependencies:
 * generated i18n, shared HTML escaping, pure tool-family/icon helpers, and a
 * trusted shared-icon port. The caller supplies already-rendered trusted
 * header slots. This owner reads no DOM, browser global, cache, or mutable
 * runtime state and never mutates its inputs.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';
import {
  safeExternalAssetUrl,
  safeImageSource,
} from './image-source-policy';
import { imageGenerationChipSvg } from './tool-round-icons';
import {
  isImageGenerationToolRound,
  plainToolStatus,
} from './tool-round-presentation';

type UnknownRecord = Readonly<Record<string, unknown>>;
type IconHtml = (
  name: string,
  size?: number | string,
  style?: string,
) => string;

export const TOOL_IMAGE_PRESENTATION_LIMITS = Object.freeze({
  descriptorsScanned: 64,
  imageTiles: 16,
});

export type ToolImageHeaderHtml = Readonly<{
  iconHtml: string;
  queryHtml: string;
  rightControlsHtml: string;
}>;

export type ToolImagePresentation = Readonly<{
  renderImageHtml(
    round: unknown,
    firstResult: unknown,
    header: ToolImageHeaderHtml,
  ): string;
}>;

export type ToolImagePresentationDependencies = Readonly<{
  translate: Translator;
  iconHtml: IconHtml;
}>;

type ImageDescriptor = Readonly<{
  source: string;
  caption: string;
}>;

type ImageDescriptorProjection = Readonly<{
  images: ImageDescriptor[];
  totalCandidates: number;
  omitted: boolean;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});
const IMAGE_TOOL_NAMES: readonly string[] = Object.freeze([
  'read_files',
  'inspect_image',
  'browser_screenshot',
  'browser_preview_page',
]);

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function stringField(value: unknown, field: string): string {
  return stringValue(record(value)[field]);
}

function projectImageDescriptors(value: unknown): ImageDescriptorProjection {
  const candidates = Array.isArray(value) ? value : [];
  const images: ImageDescriptor[] = [];
  let scanned = 0;
  let index = 0;
  while (
    index < candidates.length
    && scanned < TOOL_IMAGE_PRESENTATION_LIMITS.descriptorsScanned
    && images.length < TOOL_IMAGE_PRESENTATION_LIMITS.imageTiles
  ) {
    const candidate = record(candidates[index]);
    index += 1;
    scanned += 1;
    const source = safeImageSource(candidate.uri);
    if (!source) continue;
    images.push({
      source,
      caption: stringField(candidate, 'filename')
        || stringField(candidate, 'format'),
    });
  }
  return {
    images,
    totalCandidates: candidates.length,
    omitted: index < candidates.length,
  };
}

function localizeInspectOperations(
  translate: Translator,
  value: unknown,
  mode?: 'title',
): string {
  if (mode === 'title') return translate('inspect.opsTitle');
  const raw = stringValue(value).trim();
  if (!raw) return '';
  if (raw === 'full frame') return translate('inspect.fullFrame');
  return raw.split(',').map((segment) => {
    const operation = segment.trim();
    if (operation === 'cropped') return translate('inspect.cropped');
    if (operation === 'grid overlay') return translate('inspect.gridOverlay');
    const rotated = operation.match(/^rotated\s+(.+)$/);
    if (rotated) return translate('inspect.rotated', { deg: rotated[1] });
    const zoom = operation.match(/^zoom\s+(.+)$/);
    if (zoom) return translate('inspect.zoom', { factor: zoom[1] });
    const fitted = operation.match(/^fit to\s+(.+)$/);
    if (fitted) return translate('inspect.fitTo', { size: fitted[1] });
    return operation;
  }).join(translate('inspect.opsSep'));
}

export function createToolImagePresentation(
  dependencies: ToolImagePresentationDependencies,
): ToolImagePresentation {
  const { translate, iconHtml } = dependencies;

  function renderReadImagesHtml(
    round: UnknownRecord,
    metadata: UnknownRecord,
    header: ToolImageHeaderHtml,
  ): string {
    const toolName = stringField(round, 'toolName');
    if (!IMAGE_TOOL_NAMES.includes(toolName)) return '';
    const projection = projectImageDescriptors(metadata.imageDataUris);
    if (!projection.images.length) return '';
    const isInspect = toolName === 'inspect_image';
    const multipleImages = projection.images.length > 1;
    const tilesHtml = projection.images.map((image) => {
      const source = escapeHtmlText(image.source);
      const caption = escapeHtmlText(image.caption);
      return `<figure class="rf-img-tile">
             <img src="${source}" alt="${caption}" loading="lazy"
                  data-tofu-action="_openImageFullscreen(this.src)" />
             ${caption
               ? `<figcaption class="rf-img-cap" title="${caption}">${caption}</figcaption>`
               : ''}
           </figure>`;
    }).join('');
    const inspectOperations = stringField(metadata, 'inspectOps');
    const operationsHtml = isInspect && inspectOperations
      ? `<span class="ptool-badge rf-inspect-chip" title="${
        escapeHtmlText(localizeInspectOperations(
          translate,
          inspectOperations,
          'title',
        ))
      }">${
        escapeHtmlText(localizeInspectOperations(translate, inspectOperations))
      }</span>`
      : '';
    const displayedCount = projection.omitted
      ? projection.totalCandidates
      : projection.images.length;
    const countBadgeHtml = multipleImages
      ? `<span class="ptool-badge ptool-badge-info">${
        escapeHtmlText(translate('toolImage.images', { n: displayedCount }))
      }</span>`
      : !isInspect && metadata.badge
        ? `<span class="ptool-badge ptool-badge-info">${
          escapeHtmlText(metadata.badge)
        }</span>`
        : '';
    const limitHtml = projection.omitted
      ? `<div class="rf-img-limit">${
        escapeHtmlText(translate('toolImage.limit', {
          shown: projection.images.length,
          total: projection.totalCandidates,
        }))
      }</div>`
      : '';
    return `<div class="ptool-readimg-block${
      isInspect ? ' ptool-inspectimg-block' : ''
    }" data-rn="${escapeHtmlText(round.roundNum)}">
           <div class="ptool-line ptool-readimg-header">
             <span class="ptool-icon">${header.iconHtml}</span>
             <span class="ptool-text">${header.queryHtml}</span>
             ${operationsHtml}
             ${countBadgeHtml}
             ${header.rightControlsHtml}
           </div>
           <div class="rf-img-grid${
             multipleImages ? ' rf-img-grid-multi' : ''
           }${isInspect ? ' rf-img-grid-inspect' : ''}">${
             tilesHtml
           }</div>${limitHtml}
         </div>`;
  }

  function imageModeChipHtml(isEdit: boolean): string {
    const title = translate(
      isEdit ? 'toolImage.editedTitle' : 'toolImage.generatedTitle',
    );
    const label = translate(isEdit ? 'toolImage.edited' : 'toolImage.generated');
    return `<span class="ig-mode-chip ig-mode-chip-${
      isEdit ? 'edit' : 'gen'
    }" title="${escapeHtmlText(title)}">${
      imageGenerationChipSvg(isEdit ? 'edit' : 'generate')
    }${escapeHtmlText(label)}</span>`;
  }

  function renderGeneratedImageHtml(
    round: UnknownRecord,
    metadata: UnknownRecord,
    header: ToolImageHeaderHtml,
  ): string {
    if (!isImageGenerationToolRound(round)) return '';
    const isEdit = stringField(metadata, 'imageMode') === 'edit';
    const modeClassName = isEdit ? 'ig-mode-edit' : 'ig-mode-generate';
    const modeChipHtml = imageModeChipHtml(isEdit);
    const sourceUrl = safeImageSource(metadata.imageSourceUrl);
    const imageUri = safeImageSource(metadata.imageDataUri);
    const imageError = stringField(metadata, 'imageError');
    const prompt = stringField(metadata, 'imagePrompt')
      || stringField(round, 'query').replace(
        /^🎨\s*Generating[^:]*:\s*/i,
        '',
      );
    const aspectRatio = stringField(metadata, 'imageAspectRatio');
    const resolution = stringField(metadata, 'imageResolution');
    const parametersHtml = aspectRatio || resolution
      ? `<span class="ptool-badge ptool-badge-info ig-params">${
        aspectRatio ? escapeHtmlText(aspectRatio) : ''
      }${aspectRatio && resolution ? ' · ' : ''}${
        resolution ? escapeHtmlText(resolution) : ''
      }</span>`
      : '';

    if (imageUri) {
      const projectPath = stringField(metadata, 'imageProjectPath');
      const svgUrl = safeExternalAssetUrl(metadata.svgSavedUrl);
      const svgPath = stringField(metadata, 'svgProjectPath');
      const hasSvg = Boolean(svgUrl || svgPath);
      const svgTitle = `${translate('toolImage.svgVersion')}${
        svgPath ? `: ${svgPath}` : ''
      }`;
      const svgBadgeHtml = hasSvg
        ? `<span class="ptool-badge ptool-badge-info ig-svg-badge" title="${
          escapeHtmlText(svgTitle)
        }">SVG</span>`
        : '';
      const openSvgTitle = `${translate('toolImage.openSvg')}${
        svgPath ? ` — ${svgPath}` : ''
      }`;
      const svgButtonHtml = svgUrl
        ? `<button class="ig-action-btn" data-tofu-action="event.stopPropagation();_openExternalAsset(this)" data-url="${
          escapeHtmlText(svgUrl)
        }" title="${escapeHtmlText(openSvgTitle)}">SVG</button>`
        : '';
      const pathBadgesHtml = [
        projectPath
          ? `<span class="ig-path-chip" title="${
            escapeHtmlText(translate('toolImage.savedProject', {
              path: projectPath,
            }))
          }"><span class="ig-path-icon">${
            iconHtml('image', 12)
          }</span>${escapeHtmlText(projectPath)}</span>`
          : '',
        svgPath
          ? `<span class="ig-path-chip ig-path-chip-svg" title="${
            escapeHtmlText(translate('toolImage.svgSavedProject', {
              path: svgPath,
            }))
          }"><span class="ig-path-icon">${
            iconHtml('fileCode', 12)
          }</span>${escapeHtmlText(svgPath)}</span>`
          : '',
      ].filter(Boolean).join('');
      const pathFooterHtml = pathBadgesHtml
        ? `<div class="ig-path-bar">${pathBadgesHtml}</div>`
        : '';
      const escapedImageUri = escapeHtmlText(imageUri);
      const promptAlternativeText = escapeHtmlText(prompt.slice(0, 100));
      const imageAreaHtml = isEdit && sourceUrl
        ? `<div class="ig-beforeafter">
               <figure class="ig-ba-item">
                 <img src="${escapeHtmlText(sourceUrl)}" alt="${
                   escapeHtmlText(translate('toolImage.sourceAlt'))
                 }" loading="lazy"
                      data-tofu-action="event.stopPropagation();_openImageFullscreen(this.src)" />
                 <figcaption>${escapeHtmlText(translate('toolImage.before'))}</figcaption>
               </figure>
               <span class="ig-ba-arrow" aria-hidden="true">${
                 iconHtml('arrowRight', 16)
               }</span>
               <figure class="ig-ba-item">
                 <img src="${escapedImageUri}" alt="${promptAlternativeText}" loading="lazy"
                      data-tofu-action="event.stopPropagation();_openImageFullscreen(this.src)" />
                 <figcaption>${escapeHtmlText(translate('toolImage.after'))}</figcaption>
               </figure>
             </div>`
        : `<img src="${escapedImageUri}" alt="${promptAlternativeText}" loading="lazy"
                  data-tofu-action="_openImageFullscreen(this.src)" />`;
      const status = plainToolStatus(
        metadata.badge,
        translate('toolImage.done'),
      );
      return `<div class="ptool-imagegen-block ${modeClassName}" data-rn="${
        escapeHtmlText(round.roundNum)
      }">
           <div class="ptool-line ptool-imagegen-header">
             <span class="ptool-icon">${header.iconHtml}</span>
             <span class="ptool-text">${header.queryHtml}</span>
             ${modeChipHtml}
             ${parametersHtml}
             ${svgBadgeHtml}
             <span class="ptool-badge ptool-badge-ok">${escapeHtmlText(status)}</span>
             ${header.rightControlsHtml}
           </div>
           <div class="imagegen-card">
             ${imageAreaHtml}
             <div class="imagegen-card-footer">
               <span class="ig-prompt" title="${escapeHtmlText(prompt)}">${
                 escapeHtmlText(prompt)
               }</span>
               <div class="ig-actions">
                 ${svgButtonHtml}
                 <button type="button" class="ig-action-btn" data-tofu-action="event.stopPropagation();_downloadGenImage(this)" title="${
                   escapeHtmlText(translate('toolImage.downloadPng'))
                 }" aria-label="${
                   escapeHtmlText(translate('toolImage.downloadPng'))
                 }">${iconHtml('download', 16)}</button>
                 <button type="button" class="ig-action-btn" data-tofu-action="event.stopPropagation();_openImageFullscreen(this.closest('.imagegen-card').querySelector('.ig-beforeafter .ig-ba-item:last-child img, img').src)" title="${
                   escapeHtmlText(translate('toolImage.fullscreen'))
                 }" aria-label="${
                   escapeHtmlText(translate('toolImage.fullscreen'))
                 }">${iconHtml('maximize', 16)}</button>
               </div>
             </div>
             ${pathFooterHtml}
           </div>
         </div>`;
    }

    if (imageError) {
      const failureTitle = translate(
        isEdit ? 'toolImage.editFailed' : 'toolImage.generateFailed',
      );
      return `<div class="ptool-imagegen-block ptool-imagegen-error ${
        modeClassName
      }" data-rn="${escapeHtmlText(round.roundNum)}">
           <div class="ptool-line">
             <span class="ptool-icon">${header.iconHtml}</span>
             <span class="ptool-text">${header.queryHtml}</span>
             ${modeChipHtml}
             <span class="ptool-badge ptool-badge-err">${
               escapeHtmlText(translate('toolImage.failed'))
             }</span>
             ${header.rightControlsHtml}
           </div>
           <div class="imagegen-error">
             <div class="ig-error-title">${escapeHtmlText(failureTitle)}</div>
             <div class="ig-error-text">${escapeHtmlText(imageError)}</div>
           </div>
         </div>`;
    }

    const progressBadge = stringField(metadata, 'badge') || translate(
      isEdit ? 'toolImage.editing' : 'toolImage.generating',
    );
    const progressClassName = progressBadge.includes('rate limited')
      ? 'ptool-badge-err'
      : 'ptool-badge-warn';
    return `<div class="ptool-imagegen-block ptool-imagegen-loading ${
      modeClassName
    }" data-rn="${escapeHtmlText(round.roundNum)}">
         <div class="ptool-line ptool-active">
           <span class="ptool-icon">${header.iconHtml}</span>
           <span class="ptool-text">${header.queryHtml}</span>
           ${modeChipHtml}
           ${parametersHtml}
           <span class="ptool-badge ${progressClassName}">${
             escapeHtmlText(progressBadge)
           }</span>
           <span class="ptool-spinner"></span>
         </div>
       </div>`;
  }

  function renderImageHtml(
    roundValue: unknown,
    firstResultValue: unknown,
    header: ToolImageHeaderHtml,
  ): string {
    const round = record(roundValue);
    const metadata = record(firstResultValue);
    return renderReadImagesHtml(round, metadata, header)
      || renderGeneratedImageHtml(round, metadata, header);
  }

  return Object.freeze({ renderImageHtml });
}
