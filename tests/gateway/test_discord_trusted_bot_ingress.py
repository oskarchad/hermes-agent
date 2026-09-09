import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace
import discord

from plugins.platforms.discord.adapter import DiscordAdapter
from gateway.session import Platform, SessionSource


def _make_msg(author_id: int, is_bot: bool, mentions=None, content="hello"):
    msg = MagicMock()
    msg.id = 123456789
    msg.content = content
    msg.type = discord.MessageType.default
    author = MagicMock()
    author.id = author_id
    author.bot = is_bot
    author.name = "TestBot" if is_bot else "TestUser"
    msg.author = author
    msg.mentions = mentions or []
    msg.channel = MagicMock()
    msg.channel.id = 99999
    msg.channel.name = "general"
    msg.guild = MagicMock()
    msg.guild.id = 88888
    return msg


def test_trusted_bot_admitted_when_allow_bots_none(monkeypatch):
    """A bot in DISCORD_ALLOWED_BOTS is admitted even when DISCORD_ALLOW_BOTS=none."""
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", "111222333,444555666")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "999")

    config = MagicMock()
    config.extra = {}
    config.token = "fake"
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._client.user = MagicMock()
    adapter._client.user.id = 999999999

    # Bot in allowlist -> admitted
    trusted_bot_msg = _make_msg(author_id=111222333, is_bot=True)
    admitted, role_auth = adapter._discord_message_admission(trusted_bot_msg, claim=False)
    assert admitted is True
    assert role_auth is False

    # Untrusted bot -> rejected
    untrusted_bot_msg = _make_msg(author_id=777888999, is_bot=True)
    admitted_untrusted, _ = adapter._discord_message_admission(untrusted_bot_msg, claim=False)
    assert admitted_untrusted is False


def test_trusted_bot_inline_mention_respected(monkeypatch):
    """When bots_require_inline_mention=True, trusted bot must raw-mention the client."""
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", "111222333")
    monkeypatch.setenv("DISCORD_BOTS_REQUIRE_INLINE_MENTION", "true")

    config = MagicMock()
    config.extra = {}
    config.token = "fake"
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._client.user = MagicMock()
    adapter._client.user.id = 999999999

    # No mention -> rejected
    no_mention_msg = _make_msg(author_id=111222333, is_bot=True, content="hey there")
    admitted, _ = adapter._discord_message_admission(no_mention_msg, claim=False)
    assert admitted is False

    # Raw mention -> admitted
    mention_msg = _make_msg(author_id=111222333, is_bot=True, content="hey <@999999999> check this")
    admitted_mention, _ = adapter._discord_message_admission(mention_msg, claim=False)
    assert admitted_mention is True


def test_trusted_bot_bypasses_gateway_authz(monkeypatch):
    """_is_user_authorized allows a trusted bot from DISCORD_ALLOWED_BOTS."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)

    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", "111222333")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "999")

    trusted_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="111222333",
        user_name="ObserverBot",
        is_bot=True,
    )
    assert runner._is_user_authorized(trusted_source) is True

    untrusted_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="777888999",
        user_name="EvilBot",
        is_bot=True,
    )
    assert runner._is_user_authorized(untrusted_source) is False
