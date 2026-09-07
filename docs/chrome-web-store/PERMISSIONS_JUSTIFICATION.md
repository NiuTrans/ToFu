# Permission justifications

The Developer Dashboard (**Privacy practices** tab) shows one text box per
permission your manifest declares, labelled "Justify the use of …". Paste the
matching block below into each. Keep them concise and concrete — reviewers
reject vague answers like "needed for functionality."

These blocks correspond to the **trimmed** `manifest.store.json`, which drops
the permissions listed at the bottom of this file — the ones the current code
never calls. If you submit the original `browser_extension/manifest.json`
instead, you will be asked to justify those too — and you cannot, because they
are unused. **Submit the trimmed manifest.**

> The trim is verified by `tests/test_chrome_store_manifest_parity.py`, which
> derives the required permission set from the extension's actual `chrome.*`
> calls. Do not hand-edit `manifest.store.json` without running it.

---

## host permission: `<all_urls>`

```
The extension is a browser-automation bridge for the user's own self-hosted Tofu assistant. The user gives the assistant tasks that can involve any website (read this page, fetch a user-requested file into their Tofu server, fill this form, screenshot this dashboard), so the extension must be able to inject content scripts and read page content or a response stream on whatever URL the task targets. The set of sites cannot be known in advance, so a fixed host list is not possible.
```

## `scripting`

```
Used to inject content scripts (chrome.scripting.executeScript) into the tab the user's task targets, in order to read text, query elements, fill forms, click, hover, and send keystrokes. This is the core mechanism by which the assistant interacts with a page.
```

## `tabs`

```
Used to list the user's open tabs (titles and URLs) so they can choose which tab a task runs on, to look up a tab by id, to activate a tab before screenshotting it, and to wait for navigation to settle. No tab data is sent anywhere except back to the user's own Tofu server.
```

## `downloads`

```
The assistant can be asked explicitly to save a file in the Downloads folder of the device running Chrome. That device-local task is carried out with chrome.downloads.download, using the URL the task names and an optional filename. It returns a receipt whose location is `device_downloads`. Authenticated files requested for Tofu's server use a separate bounded response-stream transport and never call this API. The extension does not read, search, or modify the user's existing download history.
```

## `webRequest`

```
Used as the metadata-only fallback while an active Tofu browser task captures network responses for its target tab. It records URL, method, status, resource type and timing when Chrome DevTools response-body capture is unavailable. The listener is removed when the capture stops; it never reads headers, cookies, authorization values, or request bodies.
```

## `webNavigation`

```
Used to invalidate transient captured API data when the target tab navigates, changes SPA history state, or changes fragment outside an active Tofu capture. This prevents data from a prior document being returned with a later page. The extension does not record a navigation history through this API.
```

## `storage`

```
Stores the user's own configuration locally: the Tofu server URL they enter, an optional Bridge Secret, and a generated per-client id used to route commands to the right device. Nothing in storage is transmitted to any third party.
```

## `cookies`

```
Some automation tasks require reading or setting cookies for the site being automated (for example carrying a login session into a step). Used via chrome.cookies.getAll / set / remove, scoped to the URL of the task. Cookie values are returned only to the user's own Tofu server.
```

## `history`

```
Provides an optional "search my history" capability the assistant can use when a task asks it to find a page the user visited before. Invoked only on explicit task request via chrome.history.search.
```

## `bookmarks`

```
Provides an optional "read my bookmarks" capability so the assistant can open or reference a bookmarked page when a task asks for it. Read-only via chrome.bookmarks.getTree.
```

## `debugger`

```
Used for bounded, task-visible operations on one target tab: full-page screenshots; textual API/WebSocket response capture for dynamic-page research; trusted click/keyboard input; and a temporary DevTools Bridge that reads console errors, inspects JavaScript objects, and supports breakpoints/pause/step when the user's task explicitly asks for debugging. One shared per-tab attachment is reference-counted. At most two debug tabs are active, sessions expire within 120 seconds, paused pages auto-resume within 30 seconds, cross-origin child targets are detached, all console/object/source buffers have hard size limits, and the last holder detaches. It never reads request/response headers, authorization values, or request bodies.
```

## `notifications`

```
Used to show the user a desktop notification for bridge events (for example when the connection to their Tofu server drops), so they are not left wondering why automation stopped.
```

## `alarms`

```
Used to keep the MV3 service worker alive with a periodic keep-alive alarm so the long-poll connection to the user's server is not dropped when Chrome idles the worker.
```

---

## Single-purpose statement

The dashboard asks for a one-sentence single purpose. Use:

```
The single purpose of this extension is to act as a bridge that lets the user's own self-hosted Tofu AI assistant read and interact with the browser tabs the user directs it to.
```

---

## Permissions REMOVED for the store build (do not declare these)

`scripts/package_extension.sh --store` swaps in `manifest.store.json`, which
drops the following. The current `background.js` / `popup.js` never call any of
them, so they are unjustifiable and would trigger rejection:

| Removed | Why it's safe to drop |
|---|---|
| `clipboardRead` | No clipboard read anywhere (no `navigator.clipboard.readText`, no `execCommand('paste')`). |
| `clipboardWrite` | No clipboard write anywhere. |
| `declarativeNetRequest` | No DNR ruleset and no `chrome.declarativeNetRequest.*` call. |
| `management` | No `chrome.management.*` call. (This permission is a major review red flag — good to remove.) |
| `offscreen` | No `chrome.offscreen.*` call and no offscreen document. |
| `activeTab` | **Not a call-count decision** — `chrome.activeTab` is not an API, so grepping for it proves nothing. It is granted only on a *user gesture* (action click, keyboard command, context menu) and exists to widen `tabs.captureVisibleTab` to sensitive targets (`chrome://` pages, other extensions' pages, `data:` URLs). This extension has no `commands` key, no `context_menus`, and `popup.js` never captures: every command arrives from the server long-poll via `executeAndReport`, so **no gesture ever precedes a screenshot and the permission could never actually be granted**. Ordinary-page capture is covered by the `<all_urls>` host permission, which is declared. |

If you later add a feature that needs one of these, re-declare only that one
and add a justification block above.
