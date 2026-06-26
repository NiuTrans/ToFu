# Privacy policy + data-usage disclosure

The Chrome Web Store requires (1) a publicly-hosted privacy-policy URL and
(2) answers to a structured "Data usage" form. Both are below.

---

## Part 1 — Privacy policy (host this at a public URL)

Publish the text below at a stable public URL (your project site, a GitHub
Pages page, or a gist served as a page) and paste that URL into the
dashboard's **Privacy policy** field. Do not paste the text into the field —
it wants a URL.

```
Tofu Browser Bridge — Privacy Policy

Last updated: 2026-06-26

Tofu Browser Bridge ("the extension") is a companion to Tofu, an open-source,
self-hosted AI assistant that the user runs themselves. This policy explains
what the extension does with data.

WHO OPERATES THE SERVICE
There is no hosted service operated by the extension's author. The user runs
the Tofu server themselves and points the extension at it by entering a server
URL in the extension popup. The extension communicates only with that
user-supplied server.

WHAT DATA THE EXTENSION HANDLES
When the user gives their Tofu assistant a task, the extension may, in order to
carry out that task:
- read the titles and URLs of the user's open tabs;
- read the text and DOM content of a tab the task targets;
- capture a screenshot of a tab;
- read or set cookies for the site being automated;
- search the user's browsing history when a task asks it to;
- read the user's bookmarks when a task asks it to;
- interact with a page (fill forms, click, type).

WHERE THAT DATA GOES
All such data is sent only to the Tofu server URL the user configured, over the
network connection the user controls. The extension does not send data to the
extension's author, to Google beyond what Chrome itself does, or to any third
party. The user operates both ends of the connection.

WHAT IS STORED LOCALLY
The extension stores in the browser's local extension storage only: the server
URL the user entered, an optional "Bridge Secret" the user entered, and a
randomly-generated client id used to route commands to the correct device.
None of this is transmitted to any third party.

WHAT IS NOT COLLECTED
The extension does not sell data, does not use data for advertising, and does
not transfer data to anyone other than the user's own server.

USER CONTROL
The extension does nothing until the user enters a server URL and enables the
bridge. The popup provides a live connection status and a Pause control that
halts all activity. Uninstalling the extension removes all locally stored data.

CONTACT
Questions: <your contact email or project issue tracker URL>.
```

> Replace `<your contact email or project issue tracker URL>` before publishing.

---

## Part 2 — Data-usage form answers (dashboard "Privacy practices" tab)

The form has checkboxes for **what data you collect** and three **certifications**.
Answer as follows (all answers are truthful for the current code).

### "What user data do you collect?"

Check the items that apply. For a faithful disclosure of what the extension is
*capable of transmitting to the user's own server*, check:

- [x] **Website content** — page text/DOM, screenshots, and cookies of the
      tab being automated are read and sent to the user's server.
- [x] **Web history** — the `history` permission can search browsing history on
      task request.
- [ ] Personally identifiable information — not collected as a category by the
      extension itself (do not check unless you add a feature that does).
- [ ] Authentication information — cookies are handled as "website content"
      above; do not double-declare unless the store guidance for your case says to.
- [ ] Financial / payment, health, personal communications, location, user
      activity (analytics), keystroke-as-telemetry — **none collected.**

> Note: the extension transmits these to the *user's own* server, not to you.
> The store form is about what leaves the browser, so you must still declare
> the categories above even though there is no third-party recipient.

### The three required certifications

- [x] **I do not sell or transfer user data to third parties**, outside of the
      approved use cases. (True — there is no third party; data goes to the
      user's own server.)
- [x] **I do not use or transfer user data for purposes unrelated to my item's
      single purpose.** (True.)
- [x] **I do not use or transfer user data to determine creditworthiness or for
      lending purposes.** (True.)

### "Are you using remote code?"

There is a separate question about remote code. Answer **Yes**, and in the
explanation box paste:

```
The extension long-polls the user's own self-hosted Tofu server for commands. Some commands ask the extension to run a JavaScript snippet in a page the user's task targets (a browser-automation primitive equivalent to Selenium/Playwright .evaluate()). That code originates from the user's own server, which the user operates — not from a third party. The extension ships no bundled remote-code loader and pulls no code from any author-controlled endpoint.
```

> Be aware this is the highest-risk answer of the whole submission. See
> `REVIEW_RISKS.md`.
