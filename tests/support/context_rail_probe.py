"""Host and rail parts for context-rail probe Turns planted into the live page.

Production rails live inside a ConversationSurface whose root has latched its
context track (``data-turn-context-rail``). The shell grants the track only
while such a latch is mounted and not ``absent``
(``frontend/src/styles/application/23-reading-context-rail.css``), so a probe
planted straight into ``.chat-inner`` gets a zero-width rail. The host carries
only the latch, not the Surface's class, so Surface-scoped Turn styles never
reach the classic probe markup; it is ``display: contents``, so probe Turns
stay grid items of ``.chat-inner``, exactly where Surface Turns lay out.

``RAIL_PROBE_HOST`` is a JavaScript expression evaluating to the host element
(or ``null`` without a ``.chat-inner``); plants substitute it for
``__RAIL_PROBE_HOST__``.

``RAIL_PROBE_PART`` is a JavaScript function expression that places the facts
of the production renderer's ``parts.rail`` into the parts the Surface wraps
around them (``frontend/src/conversation/ui/surface-turn-renderer.ts``): the
rail track, its sticky card and the content slot. Scripts call it as
``__RAIL_PROBE_PART__(parts.rail)``. The anatomy it copies is pinned against
a real Surface render in ``frontend/src/conversation/ui/surface-design.test.ts``;
change both together.
"""

RAIL_PROBE_HOST = """(() => {
  const chat = document.querySelector('.chat-inner');
  if (!chat) return null;
  let host = chat.querySelector(':scope > .__rail-probe-host');
  if (!host) {
    host = document.createElement('section');
    host.className = '__rail-probe-host';
    host.dataset.turnContextRail = 'present';
    host.style.display = 'contents';
    chat.appendChild(host);
  }
  return host;
})()"""

RAIL_PROBE_PART = """((railHtml) => {
  const holder = document.createElement('div');
  holder.innerHTML = railHtml;
  const facts = holder.firstElementChild;
  if (!facts) return '';
  const part = (tag, name, className) => {
    const node = document.createElement(tag);
    node.dataset.conversationPart = name;
    node.className = className;
    return node;
  };
  const rail = part('aside', 'turn-context-rail', 'conversation-turn-context-rail turn-ctx');
  const card = part('div', 'turn-context-card', 'conversation-turn-context-card tctx-card');
  const content = part('div', 'turn-context-content', 'turn-context-content');
  content.append(...facts.childNodes);
  card.append(content);
  rail.append(card);
  return rail.outerHTML;
})"""


#: True once the application stylesheet styles the planted rails. The page
#: can be ready before the hashed stylesheet finishes loading, and an
#: unstyled rail is neither a track nor a bounded card, so specs wait for the
#: card's own rule before measuring.
RAIL_PROBE_STYLED = """() => {
  const card = document.querySelector('.__rail-probe-host .turn-ctx > .tctx-card');
  return !!card && getComputedStyle(card).position === 'sticky';
}"""


def with_rail_probe_part(script: str) -> str:
    """Substitute the Surface rail-part wrapper into a script."""
    assert '__RAIL_PROBE_PART__' in script, 'script does not wrap its rail in the Surface parts'
    return script.replace('__RAIL_PROBE_PART__', RAIL_PROBE_PART)


def with_rail_probe_host(script: str) -> str:
    """Substitute the host expression, and the rail-part wrapper when the plant
    uses it, into a plant script."""
    assert '__RAIL_PROBE_HOST__' in script, 'plant does not reference its rail host'
    script = script.replace('__RAIL_PROBE_HOST__', RAIL_PROBE_HOST)
    return script.replace('__RAIL_PROBE_PART__', RAIL_PROBE_PART)
