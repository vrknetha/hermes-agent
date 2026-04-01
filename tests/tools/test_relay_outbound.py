"""Tests for shared relay outbound helper."""

import pytest

from tools.relay_outbound import (
    build_relay_outbound_body,
    resolve_relay_connector_base_url,
)


def test_resolve_connector_url_prefers_explicit(monkeypatch):
    monkeypatch.setenv("RELAY_CONNECTOR_BASE_URL", "http://env.example")
    monkeypatch.setattr(
        "tools.relay_outbound._load_config_connector_base_url",
        lambda: "http://config.example",
    )
    assert (
        resolve_relay_connector_base_url("http://explicit.example")
        == "http://explicit.example"
    )


def test_resolve_connector_url_prefers_env_over_config(monkeypatch):
    monkeypatch.setenv("RELAY_CONNECTOR_BASE_URL", "http://env.example/")
    monkeypatch.setattr(
        "tools.relay_outbound._load_config_connector_base_url",
        lambda: "http://config.example",
    )
    assert resolve_relay_connector_base_url() == "http://env.example"


def test_resolve_connector_url_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("RELAY_CONNECTOR_BASE_URL", raising=False)
    monkeypatch.setattr(
        "tools.relay_outbound._load_config_connector_base_url",
        lambda: "http://config.example/",
    )
    assert resolve_relay_connector_base_url() == "http://config.example"


def test_build_body_rejects_empty_message():
    with pytest.raises(ValueError, match="empty after trimming"):
        build_relay_outbound_body(message="   ", to_agent_did="did:example:alice")


def test_build_body_rejects_missing_and_dual_targets():
    with pytest.raises(ValueError, match="Exactly one target"):
        build_relay_outbound_body(message="hello")
    with pytest.raises(ValueError, match="Exactly one target"):
        build_relay_outbound_body(
            message="hello",
            to_agent_did="did:example:alice",
            group_id="group-1",
        )


def test_build_body_direct_and_group_shapes():
    direct = build_relay_outbound_body(
        message="Hello",
        to_agent_did="did:example:alice",
        conversation_id="conv-1",
    )
    assert direct["toAgentDid"] == "did:example:alice"
    assert direct["groupId"] is None
    assert direct["conversationId"] == "conv-1"
    assert direct["payload"]["content"] == "Hello"
    assert direct["payload"]["message"] == "Hello"

    group = build_relay_outbound_body(
        message="Hello group",
        group_id="group-1",
    )
    assert group["toAgentDid"] is None
    assert group["groupId"] == "group-1"
