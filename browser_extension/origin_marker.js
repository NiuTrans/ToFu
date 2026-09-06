/* Tofu Browser Bridge — origin marker.
 *
 * Stamps THIS extension's client id onto the Tofu web app document so the
 * frontend can pin browser automation to the browser the user is actually
 * typing in. Without the marker the server sees only an anonymous fleet of
 * polling devices (two computers, one account) and any of them can win
 * routing for a given call. Registered dynamically by background.js against
 * the configured server origin; runs in the ISOLATED world but the DOM it
 * writes is shared with the page. */
(() => {
  const ATTR = 'data-tofu-browser-bridge';
  const stamp = (clientId) => {
    if (!clientId) return;
    const root = document.documentElement;
    if (root && root.getAttribute(ATTR) !== clientId) {
      root.setAttribute(ATTR, clientId);
    }
  };
  chrome.storage.local.get(['clientId'], (data) => stamp(data && data.clientId));
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.clientId) stamp(changes.clientId.newValue);
  });
})();
