"""Canonical vocabulary for the actor that initiated a conversation turn.

Writers stamp one validated ``_initiator`` value. Readers never infer identity
from feature-specific flags, which keeps attribution independent of autopilot,
scheduler, peer-message and project-brain implementation details.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

from lib.log import get_logger

logger = get_logger(__name__)

# Read-only sentinel: malformed persisted input never becomes human authority.
INITIATOR_UNKNOWN = "unknown"
INITIATOR_HUMAN = "human"
INITIATOR_AUTOPILOT = "autopilot"
INITIATOR_PROACTIVE = "proactive"
INITIATOR_TIMER = "timer"
INITIATOR_BRAIN = "brain"
INITIATOR_PEER = "peer"
INITIATOR_OPERATOR = "operator"
INITIATOR_SWARM = "swarm"
# External channel events (docs/CHANNELS.md C4/C7): third-party messages and
# owner context. Owner command-chat messages stay human with ``origin.channel``.
INITIATOR_CHANNEL = "channel"

NON_HUMAN_INITIATORS = frozenset(
    {
        INITIATOR_AUTOPILOT,
        INITIATOR_PROACTIVE,
        INITIATOR_TIMER,
        INITIATOR_BRAIN,
        INITIATOR_PEER,
        INITIATOR_OPERATOR,
        INITIATOR_SWARM,
        INITIATOR_CHANNEL,
    }
)
VALID_INITIATORS = frozenset({INITIATOR_HUMAN}) | NON_HUMAN_INITIATORS

# Program-synthesized input markers: input carrying any of them is never genuine
# human input, whether it became a Turn or was steered into a running one.
SYNTHETIC_INPUT_MARKERS = (
    '_peerMessage', '_fromConv', '_brainDispatch', '_brainEpic', '_boardTaskId',
    'boardTaskId', '_isVirtualUser', '_autopilotRunId', '_proactive',
    '_proactiveTaskId', '_timer', '_swarmAutoContinue', 'backgroundCommand',
    '_isCompactionSummary', '_isFlowReview', '_isFlowPlanner', '_flowRunId',
    '_programSynthetic',
)
STEER_BLOCK_PREFIX = 'injection:user-steer:'


def stamp_initiator(message: dict[str, Any], initiator: str) -> dict[str, Any]:
    """Stamp a controlled initiator in place and return ``message``.

    Human input remains unstamped because absence is the canonical human
    representation. Unknown initiators are rejected loudly at the write seam.
    """
    if not isinstance(message, dict):
        raise TypeError("turn message must be a dict")
    if initiator == INITIATOR_HUMAN:
        message.pop("_initiator", None)
        return message
    if not isinstance(initiator, str) or initiator not in NON_HUMAN_INITIATORS:
        raise ValueError("unknown turn initiator")
    message["_initiator"] = initiator
    return message


def resolve_initiator(message: dict[str, Any]) -> str:
    """Return the canonical initiator; malformed explicit values fail closed."""
    if not isinstance(message, Mapping):
        return INITIATOR_HUMAN
    initiator = message.get("_initiator")
    if initiator is None:
        return INITIATOR_HUMAN
    if not isinstance(initiator, str) or initiator not in VALID_INITIATORS:
        logger.warning("[Initiator] invalid persisted value type=%s", type(initiator).__name__)
        return INITIATOR_UNKNOWN
    return initiator


def resolve_turn_initiator(turn: Mapping[str, Any] | None) -> str:
    """Return the initiator of a durable Turn; malformed values fail closed.

    Turn persistence folds the transient ``_initiator`` stamp into the
    projection's ``origin.initiator`` and drops it, so a timer, peer or
    channel input is stored with ``actor='human'``. Readers of stored Turns
    use this, never ``resolve_initiator`` on the projection.
    """
    if not isinstance(turn, Mapping):
        return INITIATOR_UNKNOWN
    projection = turn.get('projection')
    if projection is None:
        return INITIATOR_HUMAN  # absence is the canonical human representation
    if not isinstance(projection, Mapping):
        return INITIATOR_UNKNOWN
    if 'origin' not in projection:
        return resolve_initiator(projection)
    origin = projection['origin']
    initiator = origin.get('initiator') if isinstance(origin, Mapping) else None
    if not isinstance(initiator, str) or initiator not in VALID_INITIATORS:
        return INITIATOR_UNKNOWN
    return initiator


def is_auto_initiated(message: dict[str, Any]) -> bool:
    """Return whether the turn was initiated outside the human input box."""
    return resolve_initiator(message) != INITIATOR_HUMAN


def steer_author(message: Any, *, actor: str) -> dict[str, Any] | None:
    """The author record ``turn.steer.commit`` stores with a human-initiated steer.

    A steered input has no Turn of its own, so its user-steer record keeps the
    facts its Turn would have had (``TurnSteerAuthor``): the input actor, the
    authored text (``originalContent`` before any input translation, else
    ``content``) and the origin, normalized exactly like a Turn projection's and
    absent for plain human input. Nonhuman, program-synthetic and text-less
    inputs record none, so they can never be forwarded as the owner's words.
    """
    if not isinstance(message, Mapping) or any(message.get(key) for key in SYNTHETIC_INPUT_MARKERS):
        return None
    original = message.get('originalContent')
    text = original if isinstance(original, str) else message.get('content')
    if not isinstance(text, str) or not text.strip():
        return None
    from lib.turn_projection_patch import normalize_projection_document
    origin = normalize_projection_document(dict(message)).get('origin')
    if origin is not None and origin.get('initiator') != INITIATOR_HUMAN:
        return None
    author: dict[str, Any] = {'actor': str(actor), 'text': text}
    if origin is not None:
        author['origin'] = origin
    return author


def steer_author_turn(author: Mapping[str, Any]) -> dict[str, Any]:
    """The Turn shape of a steer author record: its actor, its authored text as
    ``content`` and its origin, for ``require_human_turn`` and ``authored_text``."""
    projection: dict[str, Any] = {'role': 'user', 'content': author.get('text')}
    if 'origin' in author:
        projection['origin'] = author['origin']
    return {'actor': author.get('actor'), 'projection': projection}


def steered_input_turn(turn: Mapping[str, Any], steer_id: str) -> dict[str, Any]:
    """A Turn-shaped view of the input ``steer_id`` steered into ``turn``.

    The view carries only the steer's committed author record (actor, authored
    text as ``content``, origin), so ``require_human_turn`` and ``authored_text``
    judge it exactly like a Turn. Raises ValueError when the Turn has no such
    steer, when the steer has no author (nonhuman or recorded before authors
    existed) and when it missed the Turn and was queued as the next request.
    """
    projection = turn.get('projection') if isinstance(turn, Mapping) else None
    records = projection.get('_userSteerInjects') if isinstance(projection, Mapping) else None
    turn_id = turn.get('turnId') if isinstance(turn, Mapping) else None
    # turn.steer.commit writes only the live generated Turn of an attempt.
    if not isinstance(turn, Mapping) or turn.get('actor') not in ('assistant', 'planner', 'critic'):
        raise ValueError(f'Turn {turn_id} is not a reply that can carry steered messages')
    matches = [record for record in records if isinstance(record, Mapping)
               and record.get('blockId') == STEER_BLOCK_PREFIX + steer_id
               and record.get('commandId') == steer_id] if isinstance(records, list) else []
    if len(matches) != 1:
        raise ValueError(f'Turn {turn_id} has no steered message {steer_id}')
    record = matches[0]
    if record.get('deliveryState') == 'queued':
        raise ValueError(f'Steered message {steer_id} missed Turn {turn_id} and was queued as the '
                         'next request; forward that Turn instead')
    author = record.get('author')
    if (not isinstance(author, Mapping) or not isinstance(author.get('actor'), str)
            or not isinstance(author.get('text'), str)):
        raise ValueError(f'Steered message {steer_id} has no recorded human author')
    return {'conversationId': turn.get('conversationId'), 'turnId': turn_id, 'steerId': steer_id,
            **steer_author_turn(author)}


__all__ = [
    "INITIATOR_HUMAN",
    "INITIATOR_UNKNOWN",
    "INITIATOR_AUTOPILOT",
    "INITIATOR_PROACTIVE",
    "INITIATOR_TIMER",
    "INITIATOR_BRAIN",
    "INITIATOR_PEER",
    "INITIATOR_OPERATOR",
    "INITIATOR_SWARM",
    "INITIATOR_CHANNEL",
    "NON_HUMAN_INITIATORS",
    "VALID_INITIATORS",
    "SYNTHETIC_INPUT_MARKERS",
    "STEER_BLOCK_PREFIX",
    "stamp_initiator",
    "resolve_initiator",
    "resolve_turn_initiator",
    "is_auto_initiated",
    "steer_author",
    "steer_author_turn",
    "steered_input_turn",
    "require_human_turn",
]


def require_human_turn(turn: Mapping[str, Any], *, user_id: int | None = None) -> None:
    """Reject synthetic authorizations using the shared canonical actor vocabulary.

    A Global Router forward (``origin.forwardedFrom``, docs/ROUTER.md R2) is
    admitted only after owner-scoped verification of its source Turn and text,
    so callers that may meet one pass the authority owner as ``user_id``.
    """
    if not isinstance(turn, Mapping) or turn.get("actor") != "human":
        raise ValueError("Authorization source is not a human Turn")
    projection = turn.get('projection')
    if not isinstance(projection, Mapping) or resolve_initiator(projection) != 'human':
        raise ValueError('Authorization projection is not genuine human input')
    origin = projection.get('origin')
    if origin is not None and (not isinstance(origin, Mapping) or origin.get('initiator') != 'human'
            or any(origin.get(k) for k in ('sourceConversationId', 'brain', 'autopilotRunId',
                                          'boardTaskId', 'scheduledTaskId'))):
        raise ValueError('Synthetic authorization origin denied')
    if (resolve_initiator(turn) != 'human'
            or any(projection.get(k) or turn.get(k) for k in SYNTHETIC_INPUT_MARKERS)
            or projection.get('role', 'user') != 'user'
            or projection.get('actor', 'human') != 'human'):
        raise ValueError('Synthetic source cannot grant dispatch authority')
    if isinstance(origin, Mapping) and 'forwardedFrom' in origin:
        if user_id is None:
            raise ValueError('Forwarded authorization requires owner-scoped verification')
        from lib.router.forwarding import verify_forwarded_turn
        verify_forwarded_turn(turn, user_id=user_id)
