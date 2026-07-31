/* Image base64 hydrator — extracted 2026-07-26 from core/conversations.js
   (pt_3879f00e sub-part 2 slice 4). Loaded via lib/js_bundler.py::
   _BUNDLE_FILES BEFORE core/conversations.js so its two remaining call
   sites (loadConversationMessages initial-hydration branch, and its
   post-refresh path) still resolve the bare name at runtime via
   bundle-level window scope, matching slices 1-3.

   Body is byte-identical to the pre-slice inline form — comments, log
   lines, error handling, and the promise-stash contract on
   conv._hydratePromise all preserved.

   Reads at CALL time (bundle-level window scope):
     - apiUrl(...) — from api.js (for server-relative URL rewrites)

   Contract: mutates the passed conv in place — sets img.base64,
   img.mediaType (when missing), img.preview (when placeholder), and
   conv._hydratePromise (never null on return). Never throws; fetch
   failures degrade to a console.warn + a resolved promise. */

/**
 * Hydrate image base64 from server URLs.
 * After server restart, images loaded from DB only have url (base64 stripped).
 * Needed for UI rendering of images in chat messages.
 * (The backend now handles image resolution for LLM API calls independently
 * via _validate_image_blocks in conv_message_builder.py.)
 */
function _hydrateImageBase64(conv) {
  if (!conv || !conv.messages) { conv._hydratePromise = Promise.resolve(); return; }
  const promises = [];
  for (const msg of conv.messages) {
    if (!msg.images || msg.images.length === 0) continue;
    for (const img of msg.images) {
      if (img.base64) continue;  // already has base64
      const rawUrl = img.url || img.preview || "";
      if (!rawUrl || rawUrl.endsWith("...")) continue;  // truncated placeholder
      // Stored img.url is now the CANONICAL '/api/images/<f>' (no proxy
      // prefix). A bare fetch of that would bypass the reverse-proxy base
      // path, so prefix server-relative URLs with apiUrl() at fetch time.
      const url = (rawUrl.charAt(0) === "/" && typeof apiUrl === "function")
        ? apiUrl(rawUrl) : rawUrl;
      // Fetch in background — tracked via promise so message builder can use base64
      const p = fetch(url)
        .then(resp => { if (!resp.ok) throw new Error(`HTTP ${resp.status}`); return resp.blob(); })
        .then(blob => new Promise(resolve => {
          const reader = new FileReader();
          reader.onload = () => {
            const dataUrl = String(reader.result || "");
            const commaIdx = dataUrl.indexOf(",");
            if (commaIdx > 0) {
              img.base64 = dataUrl.slice(commaIdx + 1);
              if (!img.mediaType) {
                const match = dataUrl.match(/^data:([^;]+)/);
                if (match) img.mediaType = match[1];
              }
              if (!img.preview || img.preview === url)
                img.preview = dataUrl;
            }
            resolve();
          };
          reader.onerror = () => resolve();  // don't block on read errors
          reader.readAsDataURL(blob);
        }))
        .catch(e => {
          console.warn(`[hydrate] Failed to fetch base64 for image url=${url.slice(0, 80)}: ${e.message}`);
        });
      promises.push(p);
    }
  }
  if (promises.length > 0) {
    console.info(`[hydrate] Fetching base64 for ${promises.length} image(s) in conv=${conv.id.slice(0, 8)}`);
    conv._hydratePromise = Promise.all(promises).then(() => {
      console.info(`[hydrate] Completed ${promises.length} image(s) for conv=${conv.id.slice(0, 8)}`);
    });
  } else {
    conv._hydratePromise = Promise.resolve();
  }
}
