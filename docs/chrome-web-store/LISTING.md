# Store listing copy

Paste each field into the matching box in the Chrome Web Store Developer
Dashboard (**Store listing** tab).

---

## Item name (≤ 75 chars)

```
Tofu Browser Bridge
```

## Summary / short description (≤ 132 chars)

```
Connect your self-hosted Tofu AI assistant to this browser so it can read, screenshot, and act on the tabs you choose.
```

## Category

```
Workflow & Planning
```

(Alternative if rejected for category fit: **Developer Tools**.)

## Language

```
English (United States)
```

Add **Chinese (Simplified)** as a second locale if you want the zh copy shown
to CN users — the in-app UI is already bilingual.

---

## Detailed description (full)

> Paste verbatim. Plain text only — the store renders no Markdown. Keep the
> blank lines; they become paragraph breaks.

```
Tofu Browser Bridge connects your own self-hosted Tofu AI assistant to your browser.

Tofu is an open-source, self-hosted AI assistant you run on your own machine or server. This extension is the optional "bridge" that lets your Tofu assistant work with the web pages you already have open — instead of opening a separate, blank automated browser.

What it lets your assistant do, on the tabs you point it at:

• List your open tabs (titles and URLs) so you can tell it which page to work on.
• Read the text content of a tab, or a specific element by CSS selector.
• Take a full-page screenshot so the assistant can "see" the page.
• Fill in forms, click buttons, hover menus, and send keystrokes to complete multi-step tasks.
• Inspect console errors and temporarily debug JavaScript with bounded object inspection, breakpoints, pause, and stepping.
• Read or set cookies, search your history, and read your bookmarks when a task needs them.
• Transfer a file available through your browser login into bounded staging on your own Tofu server, without silently saving it to this device's Downloads folder.

WHO THIS IS FOR
This is a companion to the Tofu app. It is only useful if you are already running a Tofu server. On first use you paste your own server's URL (for example http://localhost:15000) into the extension popup. With no server configured, the extension does nothing.

HOW IT CONNECTS
The extension talks ONLY to the Tofu server URL you enter. It long-polls that server for commands and returns results. It does not send your data to us, to the extension author, or to any third party — there is no third party; you run both ends. An optional shared "Bridge Secret" can be set so only your server can drive the extension.

YOU ARE IN CONTROL
• Nothing happens until you enter your server URL and enable the bridge.
• The popup shows a live connected/disconnected status and a Pause button that stops all activity instantly.
• The assistant acts on tabs in response to tasks you give it in the Tofu app.
• DevTools sessions expire automatically, paused pages auto-resume, and executable debugging actions use Tofu's browser-domain approval controls.

OPEN SOURCE
Tofu and this extension are open source. You can read exactly what the extension does in background.js before installing.

PERMISSIONS
This extension requests broad permissions because it is a general-purpose browser-automation bridge: it has to be able to read and act on whatever page your task involves. Every permission and why it is needed is explained on the Permissions tab of this listing and in the project's documentation. If you are not comfortable granting these, simply do not install it — the rest of Tofu works without the bridge.
```

---

## Notes

- The store shows the **permission warning dialog** to users at install based
  on the manifest. The trimmed `manifest.store.json` keeps that list as short
  as honesty allows.
- Do **not** claim the extension is "by Google/Chrome" or use trademarked
  logos in screenshots — that is an instant rejection.
- The phrase "you run both ends / there is no third party" is important: it is
  the single strongest argument for the data-usage disclosure and for the
  remote-code review (the code comes from the user's own server, not a remote
  party the user doesn't control). Keep it.
