"""Tests for relay_send tool."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from tools.relay_send_tool import relay_send_tool


class _RelayRequestHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/outbound":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        self.__class__.requests.append(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):  # noqa: A003
        return


@contextmanager
def _relay_server():
    _RelayRequestHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _RelayRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _RelayRequestHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_relay_send_direct_hits_outbound_endpoint():
    with _relay_server() as (base_url, requests):
        raw = relay_send_tool(
            {
                "message": "hello direct",
                "to_agent_did": "did:example:alice",
                "conversation_id": "conv-123",
                "connector_base_url": base_url,
            }
        )
        result = json.loads(raw)

    assert result["success"] is True
    assert result["route_type"] == "direct"
    assert result["target"] == "did:example:alice"
    assert len(requests) == 1
    assert requests[0]["toAgentDid"] == "did:example:alice"
    assert requests[0]["groupId"] is None
    assert requests[0]["conversationId"] == "conv-123"
    assert requests[0]["payload"]["content"] == "hello direct"
    assert requests[0]["payload"]["message"] == "hello direct"


def test_relay_send_group_hits_outbound_endpoint():
    with _relay_server() as (base_url, requests):
        raw = relay_send_tool(
            {
                "message": "hello group",
                "group_id": "group-42",
                "conversation_id": "conv-group",
                "connector_base_url": base_url,
            }
        )
        result = json.loads(raw)

    assert result["success"] is True
    assert result["route_type"] == "group"
    assert result["target"] == "group-42"
    assert len(requests) == 1
    assert requests[0]["toAgentDid"] is None
    assert requests[0]["groupId"] == "group-42"
    assert requests[0]["conversationId"] == "conv-group"


def test_relay_send_enforces_route_xor():
    result_missing = json.loads(relay_send_tool({"message": "hello"}))
    assert result_missing["success"] is False
    assert "Exactly one" in result_missing["error"]

    result_both = json.loads(
        relay_send_tool(
            {
                "message": "hello",
                "to_agent_did": "did:example:alice",
                "group_id": "group-1",
            }
        )
    )
    assert result_both["success"] is False
    assert "Exactly one" in result_both["error"]


def test_relay_send_rejects_empty_message():
    result = json.loads(
        relay_send_tool(
            {"message": "   ", "to_agent_did": "did:example:alice"}
        )
    )
    assert result["success"] is False
    assert "cannot be empty" in result["error"]
