"""Load the sole run-message schema authority; no handwritten wire copies."""
import json
from pathlib import Path

CONTRACT = json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/swarm_messages_v1.json').read_text())
SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL = CONTRACT['tools']
LIMITS = CONTRACT['limits']
NAMESPACE = CONTRACT['namespace']
