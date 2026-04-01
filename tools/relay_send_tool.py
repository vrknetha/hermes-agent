"""Relay send tool for explicit direct/group outbound delivery."""

import json
from typing import Any, Dict

from tools.registry import registry
from tools.relay_outbound import post_relay_outbound


RELAY_SEND_SCHEMA = {
    "name": "relay_send",
    "description": (
        "Send a message through the generic outbound relay endpoint "
        "(POST /v1/outbound). Use this for explicit direct or group sends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Message text to send (required, non-empty after trim).",
            },
            "to_agent_did": {
                "type": "string",
                "description": "Direct target DID. Required for direct sends.",
            },
            "group_id": {
                "type": "string",
                "description": "Group target ID. Required for group sends.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Optional conversation ID to preserve continuity.",
            },
            "connector_base_url": {
                "type": "string",
                "description": (
                    "Optional connector base URL override. If omitted, "
                    "uses RELAY_CONNECTOR_BASE_URL or relay.connector_base_url."
                ),
            },
        },
        "required": ["message"],
    },
}


def relay_send_tool(args: Dict[str, Any], **kw) -> str:
    """Handle relay_send tool calls."""
    from model_tools import _run_async

    message = str(args.get("message", "")).strip()
    to_agent_did = str(args.get("to_agent_did", "")).strip()
    group_id = str(args.get("group_id", "")).strip()
    conversation_id = str(args.get("conversation_id", "")).strip()
    connector_base_url = str(args.get("connector_base_url", "")).strip()

    if not message:
        return json.dumps({"success": False, "error": "message is required and cannot be empty."})

    has_direct = bool(to_agent_did)
    has_group = bool(group_id)
    if has_direct == has_group:
        return json.dumps(
            {
                "success": False,
                "error": "Exactly one of to_agent_did or group_id is required.",
            }
        )

    result = _run_async(
        post_relay_outbound(
            message=message,
            to_agent_did=to_agent_did or None,
            group_id=group_id or None,
            conversation_id=conversation_id or None,
            connector_base_url=connector_base_url or None,
        )
    )

    return json.dumps(result)


registry.register(
    name="relay_send",
    toolset="messaging",
    schema=RELAY_SEND_SCHEMA,
    handler=relay_send_tool,
    emoji="📡",
)
