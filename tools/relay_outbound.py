"""Shared outbound relay HTTP helper for POST /v1/outbound."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _clean(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_config_connector_base_url() -> str:
    """Read relay.connector_base_url from top-level config.yaml."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        relay_cfg = cfg.get("relay", {}) if isinstance(cfg, dict) else {}
        if isinstance(relay_cfg, dict):
            return _clean(relay_cfg.get("connector_base_url"))
    except Exception as exc:
        logger.debug("Failed to load relay connector URL from config: %s", exc)
    return ""


def resolve_relay_connector_base_url(
    connector_base_url: Optional[str] = None,
) -> str:
    """Resolve connector base URL with precedence: arg > env > config."""
    explicit = _clean(connector_base_url)
    if explicit:
        return explicit.rstrip("/")

    env_val = _clean(os.getenv("RELAY_CONNECTOR_BASE_URL", ""))
    if env_val:
        return env_val.rstrip("/")

    from_config = _load_config_connector_base_url()
    if from_config:
        return from_config.rstrip("/")

    return ""


def build_relay_outbound_body(
    *,
    message: str,
    to_agent_did: Optional[str] = None,
    group_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build validated outbound relay payload body."""
    text = _clean(message)
    if not text:
        raise ValueError("Message content is empty after trimming.")

    target_direct = _clean(to_agent_did)
    target_group = _clean(group_id)
    has_direct = bool(target_direct)
    has_group = bool(target_group)
    if has_direct == has_group:
        raise ValueError("Exactly one target is required: toAgentDid or groupId.")

    body: Dict[str, Any] = {
        "toAgentDid": target_direct or None,
        "groupId": target_group or None,
        "conversationId": _clean(conversation_id) or None,
        "payload": {
            "content": text,
            "message": text,
        },
    }
    return body


async def post_relay_outbound(
    *,
    message: str,
    to_agent_did: Optional[str] = None,
    group_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    connector_base_url: Optional[str] = None,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """Send outbound relay request to local connector runtime."""
    try:
        body = build_relay_outbound_body(
            message=message,
            to_agent_did=to_agent_did,
            group_id=group_id,
            conversation_id=conversation_id,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    base_url = resolve_relay_connector_base_url(connector_base_url)
    if not base_url:
        return {
            "success": False,
            "error": (
                "Missing relay connector URL. Set connector_base_url, "
                "RELAY_CONNECTOR_BASE_URL, or relay.connector_base_url."
            ),
        }

    route_type = "group" if body.get("groupId") else "direct"
    target = body.get("groupId") or body.get("toAgentDid")
    endpoint = f"{base_url}/v1/outbound"

    try:
        import httpx
    except Exception:
        return {"success": False, "error": "httpx not installed"}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(endpoint, json=body)
        raw_response: Any
        try:
            raw_response = response.json()
        except Exception:
            raw_response = response.text

        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "route_type": route_type,
                "target": target,
                "conversation_id": body.get("conversationId"),
                "connector_base_url": base_url,
                "status_code": response.status_code,
                "response": raw_response,
            }

        return {
            "success": False,
            "route_type": route_type,
            "target": target,
            "conversation_id": body.get("conversationId"),
            "connector_base_url": base_url,
            "status_code": response.status_code,
            "error": f"Relay connector returned HTTP {response.status_code}",
            "response": raw_response,
        }
    except Exception as exc:
        return {
            "success": False,
            "route_type": route_type,
            "target": target,
            "conversation_id": body.get("conversationId"),
            "connector_base_url": base_url,
            "error": f"Relay outbound request failed: {exc}",
        }
