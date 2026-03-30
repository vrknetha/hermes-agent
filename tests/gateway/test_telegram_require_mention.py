"""Regression tests for Telegram group mention gating."""

import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig



def _ensure_telegram_mock():
    """Install lightweight telegram mocks when python-telegram-bot is absent."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.constants.ChatType.PRIVATE = "private"

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter, ChatType  # noqa: E402


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    config.extra["require_mention"] = True

    a = TelegramAdapter(config)
    a._bot_user_id = "9001"
    a._bot_username = "hermesbot"
    a._text_batch_delay_seconds = 0.01
    a.handle_message = AsyncMock()
    return a


async def _wait_for_flush():
    await asyncio.sleep(0.05)


def _make_message(
    *,
    text: str = "",
    chat_type: str = "group",
    reply_to_bot: bool = False,
    caption: str = None,
    document=None,
):
    if chat_type == "group":
        telegram_chat_type = ChatType.GROUP
    elif chat_type == "private":
        telegram_chat_type = ChatType.PRIVATE
    elif chat_type == "channel":
        telegram_chat_type = ChatType.CHANNEL
    else:
        telegram_chat_type = chat_type

    chat = SimpleNamespace(
        id=12345,
        type=telegram_chat_type,
        title="Test Group" if chat_type == "group" else None,
        full_name="Test User",
    )
    user = SimpleNamespace(id=42, full_name="User 42")

    reply_to_message = None
    if reply_to_bot:
        reply_to_message = SimpleNamespace(
            message_id=777,
            text="bot says hi",
            caption=None,
            from_user=SimpleNamespace(id=9001, full_name="Hermes Bot"),
        )

    return SimpleNamespace(
        message_id=101,
        text=text,
        caption=caption,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        message_thread_id=None,
        reply_to_message=reply_to_message,
        entities=None,
        caption_entities=None,
        forum_topic_created=None,
        media_group_id=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        document=document,
    )


def _make_update(message):
    return SimpleNamespace(message=message)


@pytest.mark.asyncio
async def test_group_text_without_mention_reply_or_prefix_is_ignored(adapter):
    update = _make_update(_make_message(text="hello everyone", chat_type="group"))

    await adapter._handle_text_message(update, MagicMock())
    await _wait_for_flush()

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_text_with_bot_mention_is_processed_and_cleaned(adapter):
    update = _make_update(_make_message(text="@hermesbot summarize this", chat_type="group"))

    await adapter._handle_text_message(update, MagicMock())
    await _wait_for_flush()

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert "@hermesbot" not in event.text.lower()
    assert "summarize this" in event.text.lower()


@pytest.mark.asyncio
async def test_group_reply_to_bot_is_processed(adapter):
    update = _make_update(_make_message(text="follow up", chat_type="group", reply_to_bot=True))

    await adapter._handle_text_message(update, MagicMock())
    await _wait_for_flush()

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_text_with_trigger_prefix_is_processed(adapter):
    adapter.config.extra["trigger_prefixes"] = ["/"]
    update = _make_update(_make_message(text="/status", chat_type="group"))

    await adapter._handle_text_message(update, MagicMock())
    await _wait_for_flush()

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_command_without_targeting_is_ignored_when_strict(adapter):
    update = _make_update(_make_message(text="/status", chat_type="group"))

    await adapter._handle_command(update, MagicMock())

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_command_with_bot_suffix_is_treated_as_targeted(adapter):
    update = _make_update(_make_message(text="/status@hermesbot", chat_type="group"))

    await adapter._handle_command(update, MagicMock())

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/status"


@pytest.mark.asyncio
async def test_group_command_with_bot_suffix_is_case_insensitive(adapter):
    adapter._bot_username = "otherbot"
    update = _make_update(_make_message(text="/status@OtherBot", chat_type="group"))

    await adapter._handle_command(update, MagicMock())

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/status"


@pytest.mark.asyncio
async def test_group_command_targeting_other_bot_is_ignored(adapter):
    update = _make_update(_make_message(text="/status@otherbot", chat_type="group"))

    await adapter._handle_command(update, MagicMock())

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_message_is_processed_without_mention(adapter):
    update = _make_update(_make_message(text="hello in dm", chat_type="private"))

    await adapter._handle_text_message(update, MagicMock())
    await _wait_for_flush()

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_non_text_document_without_targeting_is_ignored(adapter):
    unsupported_doc = SimpleNamespace(
        file_name="payload.exe",
        mime_type="application/octet-stream",
        file_size=1024,
    )
    update = _make_update(
        _make_message(text="", chat_type="group", document=unsupported_doc)
    )

    await adapter._handle_media_message(update, MagicMock())

    adapter.handle_message.assert_not_awaited()
