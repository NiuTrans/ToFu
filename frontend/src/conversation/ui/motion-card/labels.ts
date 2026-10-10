/** Localized titles, purpose captions and source chips; no runtime or DOM. */
import type { I18nKey } from "../../../i18n";

export const MOTION_CARD_TITLES: Readonly<Record<string, readonly [I18nKey, string]>> = {
  motion_video_env_check:        ["motionHead.envCheck", "Checked the video toolchain"],
  motion_video_storyboard_check: ["motionHead.storyboard", "Validated the storyboard against its transcript"],
  motion_video_check:            ["motionHead.check", "Ran the static quality gates on a scene"],
  motion_video_render:           ["motionHead.render", "Rendered a scene to video"],
  motion_video_probe:            ["motionHead.probe", "Inspected a media file"],
  motion_video_concat:           ["motionHead.concat", "Assembled the scenes into the final video"],
  motion_video_narrate:          ["motionHead.narrate", "Synthesized the narration track"],
  motion_video_mux:              ["motionHead.mux", "Merged narration into the video"],
  produce_video:                 ["motionHead.produceVideo", "Started a video job"],
  produce_report:                ["motionHead.produceReport", "Started a research report"],
  produce_research:              ["motionHead.produceResearch", "Started a research survey"],
  produce_slides:                ["motionHead.produceSlides", "Started a slide-deck job"],
  edit_slides:                   ["motionHead.editSlides", "Edited one page of a deck"],
  edit_video:                    ["motionHead.editVideo", "Edited one scene of a video"],
  patch_slides:                  ["motionHead.patchSlides", "Patched one page of a deck"],
  restyle_slides:                ["motionHead.restyleSlides", "Restyled the whole deck"],
  revert_slides:                 ["motionHead.revertSlides", "Reverted the deck to a snapshot"],
  revert_video:                  ["motionHead.revertVideo", "Reverted one scene to a snapshot"],
};
export const MOTION_CARD_PURPOSES: Readonly<Record<string, readonly [I18nKey, string]>> = {
  motion_video_env_check:        ["motionWhy.envCheck", "Confirms Node / HyperFrames / ffmpeg / headless Chrome are all present before any render work starts."],
  motion_video_storyboard_check: ["motionWhy.storyboard", "Zero-LLM gate: the storyboard must cover the narration transcript end-to-end before any scene may render."],
  motion_video_check:            ["motionWhy.check", "Lint + headless-Chrome runtime check + layout inspection. A scene must pass before it is allowed to render."],
  motion_video_render:           ["motionWhy.render", "Renders one scene composition to MP4 via headless Chrome (deterministic)."],
  motion_video_probe:            ["motionWhy.probe", "Reads codec / resolution / fps / duration / audio-track from a media file — the post-render spec check."],
  motion_video_concat:           ["motionWhy.concat", "Joins the per-scene MP4s into one final video; mismatched scenes are normalized to the first scene's spec."],
  motion_video_narrate:          ["motionWhy.narrate", "Synthesizes one narration WAV per scene and reports the duration each scene must become (alignment manifest)."],
  motion_video_mux:              ["motionWhy.mux", "Copies the video stream and merges the narration track as normalized AAC — the deliverable file."],
  produce_video:                 ["motionWhy.produceVideo", "One sentence → finished narrated video. The job runs in the background; progress shows in the video panel."],
  produce_report:                ["motionWhy.produceReport", "One sentence → a cited long-form report, published as a markdown artifact when it finishes."],
  produce_research:              ["motionWhy.produceResearch", "Harvests the recent literature, then proposes and scores genuinely-novel research ideas against it."],
  produce_slides:                ["motionWhy.produceSlides", "One sentence → a designer-quality editable PPTX: scenario theme + bound palette/typefaces + per-page layout + visual QA. Downloads when done."],
  edit_slides:                   ["motionWhy.editSlides", "Re-authors ONE page of a produced deck from your plain-language instruction, then re-renders the preview and re-exports the PPTX."],
  edit_video:                    ["motionWhy.editVideo", "Rewrites ONE scene's composition from your plain-language instruction (the better of old/new wins), re-renders just that scene, and re-assembles the final video at its original URL."],
  patch_slides:                  ["motionWhy.patchSlides", "Deterministic field-level edits (no LLM): text, style, geometry, image, delete, duplicate — validated and layout-measured, rolled back on regression."],
  restyle_slides:                ["motionWhy.restyleSlides", "Deck-level token swap (no LLM): bind a curated theme, override palette slots, or swap typefaces — every page re-renders and the PPTX re-exports."],
  revert_slides:                 ["motionWhy.revertSlides", "Restores the deck snapshot taken before an edit — re-validated, re-rendered, re-exported. Snapshots are kept, so you can bounce between versions."],
  revert_video:                  ["motionWhy.revertVideo", "Restores one scene's composition snapshot from before an edit — only that scene re-renders and the film re-assembles at its original URL."],
};

/* Per-tool source chip — the shared "Video" chip lied about three of the
 * four produce_* cards (a slides job is not a video). Tools absent here keep
 * the historical motionSrc chip (the motion_video_* family IS video work). */
export const MOTION_CARD_SOURCES: Readonly<Record<string, readonly [I18nKey, string]>> = {
  produce_report:   ["motionSrcReport", "Report"],
  produce_research: ["motionSrcResearch", "Research"],
  produce_slides:   ["motionSrcSlides", "Slides"],
  edit_slides:      ["motionSrcSlides", "Slides"],
  patch_slides:     ["motionSrcSlides", "Slides"],
  restyle_slides:   ["motionSrcSlides", "Slides"],
  revert_slides:    ["motionSrcSlides", "Slides"],
};

/* The motion handler's meta.title is the bare stage name ("check" / "render")
 * — noisy inside the card, so the motion body never renders a snippet. The
 * produce handlers' note / quality_hint DO carry user-meaningful text. */
