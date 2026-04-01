"""Unit tests for the generic webhook platform adapter.

Covers:
- HMAC signature validation (GitHub, GitLab, generic)
- Prompt rendering with dot-notation template variables
- Event type filtering
- HTTP handler behaviour (404, 202, health)
- Idempotency cache (duplicate delivery IDs)
- Rate limiting (fixed-window, per route)
- Body size limits
- INSECURE_NO_AUTH bypass
- Session isolation for concurrent webhooks
- Optional sticky session key header for thread continuity
- Delivery info cleanup after send()
- connect / disconnect lifecycle
"""

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    _INSECURE_NO_AUTH,
    check_webhook_requirements,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    routes=None,
    secret="",
    rate_limit=30,
    max_body_bytes=1_048_576,
    host="0.0.0.0",
    port=0,  # let OS pick a free port in tests
):
    """Build a PlatformConfig suitable for WebhookAdapter."""
    extra = {
        "host": host,
        "port": port,
        "routes": routes or {},
        "rate_limit": rate_limit,
        "max_body_bytes": max_body_bytes,
    }
    if secret:
        extra["secret"] = secret
    return PlatformConfig(enabled=True, extra=extra)


def _make_adapter(routes=None, **kwargs):
    """Create a WebhookAdapter with sensible defaults for testing."""
    config = _make_config(routes=routes, **kwargs)
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    """Build the aiohttp Application from the adapter (without starting a full server)."""
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _mock_request(headers=None, body=b"", content_length=None, match_info=None):
    """Build a lightweight mock aiohttp request for non-HTTP tests."""
    req = MagicMock()
    req.headers = headers or {}
    req.content_length = content_length if content_length is not None else len(body)
    req.match_info = match_info or {}
    req.method = "POST"

    async def _read():
        return body

    req.read = _read
    return req


def _github_signature(body: bytes, secret: str) -> str:
    """Compute X-Hub-Signature-256 for *body* using *secret*."""
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


def _generic_signature(body: bytes, secret: str) -> str:
    """Compute X-Webhook-Signature (plain HMAC-SHA256 hex) for *body*."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ===================================================================
# Signature validation
# ===================================================================


class TestValidateSignature:
    """Tests for WebhookAdapter._validate_signature."""

    def test_validate_github_signature_valid(self):
        """Valid X-Hub-Signature-256 is accepted."""
        adapter = _make_adapter()
        body = b'{"action": "opened"}'
        secret = "webhook-secret-42"
        sig = _github_signature(body, secret)
        req = _mock_request(headers={"X-Hub-Signature-256": sig})
        assert adapter._validate_signature(req, body, secret) is True

    def test_validate_github_signature_invalid(self):
        """Wrong X-Hub-Signature-256 is rejected."""
        adapter = _make_adapter()
        body = b'{"action": "opened"}'
        secret = "webhook-secret-42"
        req = _mock_request(headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        assert adapter._validate_signature(req, body, secret) is False

    def test_validate_gitlab_token(self):
        """GitLab plain-token match via X-Gitlab-Token."""
        adapter = _make_adapter()
        secret = "gl-token-value"
        req = _mock_request(headers={"X-Gitlab-Token": secret})
        assert adapter._validate_signature(req, b"{}", secret) is True

    def test_validate_gitlab_token_wrong(self):
        """Wrong X-Gitlab-Token is rejected."""
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Gitlab-Token": "wrong"})
        assert adapter._validate_signature(req, b"{}", "correct") is False

    def test_validate_no_signature_with_secret_rejects(self):
        """Secret configured but no recognised signature header → reject."""
        adapter = _make_adapter()
        req = _mock_request(headers={})  # no sig headers at all
        assert adapter._validate_signature(req, b"{}", "my-secret") is False

    def test_validate_no_secret_allows_all(self):
        """When the secret is empty/falsy, the validator is never even called
        by the handler (secret check is 'if secret and secret != _INSECURE...').
        Verify that an empty secret isn't accidentally passed to the validator."""
        # This tests the semantics: empty secret means skip validation entirely.
        # The handler code does: if secret and secret != _INSECURE_NO_AUTH: validate
        # So with an empty secret, _validate_signature is never reached.
        # We just verify the code path is correct by constructing an adapter
        # with no secret and confirming the route config resolves to "".
        adapter = _make_adapter(
            routes={"test": {"prompt": "hello"}},
            secret="",
        )
        # The route has no secret, global secret is empty
        route_secret = adapter._routes["test"].get("secret", adapter._global_secret)
        assert not route_secret  # empty → validation is skipped in handler

    def test_validate_generic_signature_valid(self):
        """Valid X-Webhook-Signature (generic HMAC-SHA256 hex) is accepted."""
        adapter = _make_adapter()
        body = b'{"event": "push"}'
        secret = "generic-secret"
        sig = _generic_signature(body, secret)
        req = _mock_request(headers={"X-Webhook-Signature": sig})
        assert adapter._validate_signature(req, body, secret) is True


# ===================================================================
# Prompt rendering
# ===================================================================


class TestRenderPrompt:
    """Tests for WebhookAdapter._render_prompt."""

    def test_render_prompt_dot_notation(self):
        """Dot-notation {pull_request.title} resolves nested keys."""
        adapter = _make_adapter()
        payload = {"pull_request": {"title": "Fix bug", "number": 42}}
        result = adapter._render_prompt(
            "PR #{pull_request.number}: {pull_request.title}",
            payload,
            "pull_request",
            "github",
        )
        assert result == "PR #42: Fix bug"

    def test_render_prompt_missing_key_preserved(self):
        """{nonexistent} is left as-is when key doesn't exist in payload."""
        adapter = _make_adapter()
        result = adapter._render_prompt(
            "Hello {nonexistent}!",
            {"action": "opened"},
            "push",
            "test",
        )
        assert "{nonexistent}" in result

    def test_render_prompt_no_template_dumps_json(self):
        """Empty template → JSON dump fallback with event/route context."""
        adapter = _make_adapter()
        payload = {"key": "value"}
        result = adapter._render_prompt("", payload, "push", "my-route")
        assert "push" in result
        assert "my-route" in result
        assert "key" in result


# ===================================================================
# Delivery extra rendering
# ===================================================================


class TestRenderDeliveryExtra:
    def test_render_delivery_extra_templates(self):
        """String values in deliver_extra are rendered with payload data."""
        adapter = _make_adapter()
        extra = {"repo": "{repository.full_name}", "pr_number": "{number}", "static": 42}
        payload = {"repository": {"full_name": "org/repo"}, "number": 7}
        result = adapter._render_delivery_extra(extra, payload)
        assert result["repo"] == "org/repo"
        assert result["pr_number"] == "7"
        assert result["static"] == 42  # non-string left as-is


# ===================================================================
# Event filtering
# ===================================================================


class TestEventFilter:
    """Tests for event type filtering in _handle_webhook."""

    @pytest.mark.asyncio
    async def test_event_filter_accepts_matching(self):
        """Matching event type passes through."""
        routes = {
            "gh": {
                "secret": _INSECURE_NO_AUTH,
                "events": ["pull_request"],
                "prompt": "PR: {action}",
            }
        }
        adapter = _make_adapter(routes=routes)
        # Stub handle_message to avoid running the agent
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/gh",
                json={"action": "opened"},
                headers={"X-GitHub-Event": "pull_request"},
            )
            assert resp.status == 202

    @pytest.mark.asyncio
    async def test_event_filter_rejects_non_matching(self):
        """Non-matching event type returns 200 with status=ignored."""
        routes = {
            "gh": {
                "secret": _INSECURE_NO_AUTH,
                "events": ["pull_request"],
                "prompt": "test",
            }
        }
        adapter = _make_adapter(routes=routes)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/gh",
                json={"action": "opened"},
                headers={"X-GitHub-Event": "push"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_event_filter_empty_allows_all(self):
        """No events list → accept any event type."""
        routes = {
            "all": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "got it",
            }
        }
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/all",
                json={"action": "any"},
                headers={"X-GitHub-Event": "whatever"},
            )
            assert resp.status == 202


# ===================================================================
# HTTP handling
# ===================================================================


class TestHTTPHandling:

    @pytest.mark.asyncio
    async def test_unknown_route_returns_404(self):
        """POST to an unknown route returns 404."""
        adapter = _make_adapter(routes={"real": {"secret": _INSECURE_NO_AUTH, "prompt": "x"}})
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/webhooks/nonexistent", json={"a": 1})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_webhook_handler_returns_202(self):
        """Valid request returns 202 Accepted."""
        routes = {"test": {"secret": _INSECURE_NO_AUTH, "prompt": "hi"}}
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/webhooks/test", json={"data": "value"})
            assert resp.status == 202
            data = await resp.json()
            assert data["status"] == "accepted"
            assert data["route"] == "test"

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """GET /health returns 200 with status=ok."""
        adapter = _make_adapter()
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["platform"] == "webhook"

    @pytest.mark.asyncio
    async def test_connect_starts_server(self):
        """connect() starts the HTTP listener and marks adapter as connected."""
        routes = {"r1": {"secret": _INSECURE_NO_AUTH, "prompt": "x"}}
        adapter = _make_adapter(routes=routes, port=0)
        # Use port 0 — the OS picks a free port, but aiohttp requires a real bind.
        # We just test that the method completes and marks connected.
        # Need to mock TCPSite to avoid actual binding.
        with patch("gateway.platforms.webhook.web.AppRunner") as MockRunner, \
             patch("gateway.platforms.webhook.web.TCPSite") as MockSite:
            mock_runner_inst = AsyncMock()
            MockRunner.return_value = mock_runner_inst
            mock_site_inst = AsyncMock()
            MockSite.return_value = mock_site_inst

            result = await adapter.connect()
            assert result is True
            assert adapter.is_connected
            mock_runner_inst.setup.assert_awaited_once()
            mock_site_inst.start.assert_awaited_once()

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self):
        """disconnect() stops the server and marks adapter disconnected."""
        adapter = _make_adapter()
        # Simulate a runner that was previously set up
        mock_runner = AsyncMock()
        adapter._runner = mock_runner
        adapter._running = True

        await adapter.disconnect()
        mock_runner.cleanup.assert_awaited_once()
        assert adapter._runner is None
        assert not adapter.is_connected


# ===================================================================
# Idempotency
# ===================================================================


class TestIdempotency:

    @pytest.mark.asyncio
    async def test_duplicate_delivery_id_returns_200(self):
        """Second request with same delivery ID returns 200 duplicate."""
        routes = {"idem": {"secret": _INSECURE_NO_AUTH, "prompt": "test"}}
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers = {"X-GitHub-Delivery": "delivery-123"}
            resp1 = await cli.post("/webhooks/idem", json={"a": 1}, headers=headers)
            assert resp1.status == 202

            resp2 = await cli.post("/webhooks/idem", json={"a": 1}, headers=headers)
            assert resp2.status == 200
            data = await resp2.json()
            assert data["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_expired_delivery_id_allows_reprocess(self):
        """After TTL expires, the same delivery ID is accepted again."""
        routes = {"idem": {"secret": _INSECURE_NO_AUTH, "prompt": "test"}}
        adapter = _make_adapter(routes=routes)
        adapter._idempotency_ttl = 1  # 1 second TTL for test speed
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers = {"X-GitHub-Delivery": "delivery-456"}

            resp1 = await cli.post("/webhooks/idem", json={"x": 1}, headers=headers)
            assert resp1.status == 202

            # Backdate the cache entry so it appears expired
            adapter._seen_deliveries["delivery-456"] = time.time() - 3700

            resp2 = await cli.post("/webhooks/idem", json={"x": 1}, headers=headers)
            assert resp2.status == 202  # re-accepted


# ===================================================================
# Rate limiting
# ===================================================================


class TestRateLimiting:

    @pytest.mark.asyncio
    async def test_rate_limit_rejects_excess(self):
        """Exceeding the rate limit returns 429."""
        routes = {"limited": {"secret": _INSECURE_NO_AUTH, "prompt": "test"}}
        adapter = _make_adapter(routes=routes, rate_limit=2)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # Two requests within limit
            for i in range(2):
                resp = await cli.post(
                    "/webhooks/limited",
                    json={"n": i},
                    headers={"X-GitHub-Delivery": f"d-{i}"},
                )
                assert resp.status == 202, f"Request {i} should be accepted"

            # Third request should be rate-limited
            resp = await cli.post(
                "/webhooks/limited",
                json={"n": 99},
                headers={"X-GitHub-Delivery": "d-99"},
            )
            assert resp.status == 429

    @pytest.mark.asyncio
    async def test_rate_limit_window_resets(self):
        """After the 60-second window passes, requests are allowed again."""
        routes = {"limited": {"secret": _INSECURE_NO_AUTH, "prompt": "test"}}
        adapter = _make_adapter(routes=routes, rate_limit=1)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/limited",
                json={"n": 1},
                headers={"X-GitHub-Delivery": "d-a"},
            )
            assert resp.status == 202

            # Backdate all rate-limit timestamps to > 60 seconds ago
            adapter._rate_counts["limited"] = [time.time() - 120]

            resp = await cli.post(
                "/webhooks/limited",
                json={"n": 2},
                headers={"X-GitHub-Delivery": "d-b"},
            )
            assert resp.status == 202  # allowed again


# ===================================================================
# Body size limit
# ===================================================================


class TestBodySize:

    @pytest.mark.asyncio
    async def test_oversized_payload_rejected(self):
        """Content-Length > max_body_bytes returns 413."""
        routes = {"big": {"secret": _INSECURE_NO_AUTH, "prompt": "test"}}
        adapter = _make_adapter(routes=routes, max_body_bytes=100)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            large_payload = {"data": "x" * 200}
            resp = await cli.post(
                "/webhooks/big",
                json=large_payload,
                headers={"Content-Length": "999999"},
            )
            assert resp.status == 413


# ===================================================================
# INSECURE_NO_AUTH
# ===================================================================


class TestInsecureNoAuth:

    @pytest.mark.asyncio
    async def test_insecure_no_auth_skips_validation(self):
        """Setting secret to _INSECURE_NO_AUTH bypasses signature check."""
        routes = {"open": {"secret": _INSECURE_NO_AUTH, "prompt": "hello"}}
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # No signature header at all — should still be accepted
            resp = await cli.post("/webhooks/open", json={"test": True})
            assert resp.status == 202


# ===================================================================
# Session isolation
# ===================================================================


class TestSessionIsolation:

    @pytest.mark.asyncio
    async def test_concurrent_webhooks_get_independent_sessions(self):
        """Two events on the same route produce different session keys."""
        routes = {"ci": {"secret": _INSECURE_NO_AUTH, "prompt": "build"}}
        adapter = _make_adapter(routes=routes)

        captured_events = []

        async def _capture(event):
            captured_events.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp1 = await cli.post(
                "/webhooks/ci",
                json={"ref": "main"},
                headers={"X-GitHub-Delivery": "aaa-111"},
            )
            assert resp1.status == 202

            resp2 = await cli.post(
                "/webhooks/ci",
                json={"ref": "dev"},
                headers={"X-GitHub-Delivery": "bbb-222"},
            )
            assert resp2.status == 202

        # Wait for the async tasks to be created
        await asyncio.sleep(0.05)

        assert len(captured_events) == 2
        ids = {ev.source.chat_id for ev in captured_events}
        assert len(ids) == 2, "Each delivery must have a unique session chat_id"

    @pytest.mark.asyncio
    async def test_session_key_header_reuses_same_session(self):
        """X-Webhook-Session-Key pins events to one session chat_id."""
        routes = {"ci": {"secret": _INSECURE_NO_AUTH, "prompt": "build"}}
        adapter = _make_adapter(routes=routes)

        captured_events = []

        async def _capture(event):
            captured_events.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers_1 = {
                "X-GitHub-Delivery": "sess-111",
                "X-Webhook-Session-Key": "group:alpha",
            }
            resp1 = await cli.post("/webhooks/ci", json={"ref": "main"}, headers=headers_1)
            assert resp1.status == 202

            headers_2 = {
                "X-GitHub-Delivery": "sess-222",
                "X-Webhook-Session-Key": "group:alpha",
            }
            resp2 = await cli.post("/webhooks/ci", json={"ref": "dev"}, headers=headers_2)
            assert resp2.status == 202

        await asyncio.sleep(0.05)

        assert len(captured_events) == 2
        ids = {ev.source.chat_id for ev in captured_events}
        assert ids == {"webhook:ci:group:alpha"}

    @pytest.mark.asyncio
    async def test_session_key_header_keeps_idempotency_by_delivery_id(self):
        """Duplicate delivery IDs are still dropped even with sticky session key."""
        routes = {"ci": {"secret": _INSECURE_NO_AUTH, "prompt": "build"}}
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers = {
                "X-GitHub-Delivery": "dup-111",
                "X-Webhook-Session-Key": "group:alpha",
            }
            first = await cli.post("/webhooks/ci", json={"ref": "main"}, headers=headers)
            assert first.status == 202

            duplicate = await cli.post("/webhooks/ci", json={"ref": "main"}, headers=headers)
            assert duplicate.status == 200
            body = await duplicate.json()
            assert body["status"] == "duplicate"


# ===================================================================
# Delivery info cleanup
# ===================================================================


class TestDeliveryCleanup:

    @pytest.mark.asyncio
    async def test_delivery_info_cleaned_after_send(self):
        """send() pops delivery_info so the entry doesn't leak memory."""
        adapter = _make_adapter()
        chat_id = "webhook:test:d-xyz"
        adapter._delivery_info[chat_id] = [
            {
                "delivery_id": "d-xyz",
                "deliver": "log",
                "deliver_extra": {},
                "payload": {"x": 1},
            }
        ]

        result = await adapter.send(chat_id, "Agent response here", reply_to="d-xyz")
        assert result.success is True
        assert chat_id not in adapter._delivery_info


class TestRelayHttpDeliveryFailures:
    @pytest.mark.asyncio
    async def test_missing_connector_url_fails_without_send(self, monkeypatch):
        monkeypatch.delenv("RELAY_CONNECTOR_BASE_URL", raising=False)
        routes = {
            "relay": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "x",
                "deliver": "relay_http",
            }
        }
        adapter = _make_adapter(routes=routes)
        chat_id = "webhook:relay:d-1"
        adapter._delivery_info[chat_id] = [
            {
                "delivery_id": "d-1",
                "deliver": "relay_http",
                "deliver_extra": {},
                "payload": {"sender_did": "did:example:alice"},
            }
        ]

        # Ensure top-level config fallback is empty in this test process.
        with patch("tools.relay_outbound._load_config_connector_base_url", return_value=""):
            result = await adapter.send(chat_id, "hello", reply_to="d-1")

        assert result.success is False
        assert "Missing relay connector URL" in (result.error or "")

    @pytest.mark.asyncio
    async def test_missing_route_metadata_fails_before_http_call(self):
        adapter = _make_adapter()
        chat_id = "webhook:relay:d-2"
        adapter._delivery_info[chat_id] = [
            {
                "delivery_id": "d-2",
                "deliver": "relay_http",
                "deliver_extra": {},
                "payload": {},
            }
        ]

        with patch(
            "gateway.platforms.webhook.post_relay_outbound",
            new=AsyncMock(return_value={"success": True}),
        ) as relay_mock:
            result = await adapter.send(chat_id, "hello", reply_to="d-2")

        assert result.success is False
        assert "metadata.groupId and sender_did" in (result.error or "")
        relay_mock.assert_not_awaited()


class TestStickySessionDeliveryQueue:
    @pytest.mark.asyncio
    async def test_sticky_session_preserves_per_delivery_metadata(self):
        routes = {
            "relay": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "reply",
                "deliver": "relay_http",
            }
        }
        adapter = _make_adapter(routes=routes)
        adapter.handle_message = AsyncMock()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            headers_a = {
                "X-GitHub-Delivery": "d-a",
                "X-Webhook-Session-Key": "shared",
            }
            resp_a = await cli.post(
                "/webhooks/relay",
                json={"sender_did": "did:example:a"},
                headers=headers_a,
            )
            assert resp_a.status == 202

            headers_b = {
                "X-GitHub-Delivery": "d-b",
                "X-Webhook-Session-Key": "shared",
            }
            resp_b = await cli.post(
                "/webhooks/relay",
                json={"sender_did": "did:example:b"},
                headers=headers_b,
            )
            assert resp_b.status == 202

        chat_id = "webhook:relay:shared"
        queue = adapter._delivery_info.get(chat_id, [])
        assert len(queue) == 2

        relay_mock = AsyncMock(
            side_effect=[
                {"success": True, "route_type": "direct", "target": "did:example:a"},
                {"success": True, "route_type": "direct", "target": "did:example:b"},
            ]
        )

        async def _send_once(reply_to: str, text: str):
            return await adapter.send(chat_id, text, reply_to=reply_to)

        with patch("gateway.platforms.webhook.post_relay_outbound", new=relay_mock):
            result_a = await asyncio.create_task(_send_once("d-a", "resp-a"))
            result_b = await asyncio.create_task(_send_once("d-b", "resp-b"))

        assert result_a.success is True
        assert result_b.success is True
        assert relay_mock.await_count == 2
        assert relay_mock.await_args_list[0].kwargs["to_agent_did"] == "did:example:a"
        assert relay_mock.await_args_list[1].kwargs["to_agent_did"] == "did:example:b"
        assert chat_id not in adapter._delivery_info


# ===================================================================
# check_webhook_requirements
# ===================================================================


class TestCheckRequirements:
    def test_returns_true_when_aiohttp_available(self):
        assert check_webhook_requirements() is True

    @patch("gateway.platforms.webhook.AIOHTTP_AVAILABLE", False)
    def test_returns_false_without_aiohttp(self):
        assert check_webhook_requirements() is False
