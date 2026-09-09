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


def test_f1_trusted_bot_has_no_gateway_control_and_cannot_approve(monkeypatch):
    """F1: A trusted bot admitted via DISCORD_ALLOWED_BOTS must have allow_gateway_control=False
    and must NEVER be allowed to approve pending commands via plaintext or slash handlers."""
    import asyncio
    from datetime import datetime, timezone
    from gateway.config import GatewayConfig, PlatformConfig, Platform
    from gateway.session import SessionSource
    from gateway.run import GatewayRunner
    from tools.approval import _gateway_queues
    from tools.approval_gateway_wait import _ApprovalEntry

    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", "888")
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "999")
    monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")

    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={"history_backfill": False}))
    me = SimpleNamespace(id=777, bot=True)
    adapter._client = SimpleNamespace(user=me)
    adapter._ready_event.set()
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = MagicMock(return_value=asyncio.sleep(0))

    channel = MagicMock(spec=discord.Thread)
    channel.id = 100
    channel.parent_id = 101
    channel.name = "pilot"
    channel.topic = None
    channel.parent = SimpleNamespace(id=101, name="test", topic=None)
    channel.owner_id = 999

    author = SimpleNamespace(id=888, bot=True, name="ObserverBot", display_name="ObserverBot")
    msg = SimpleNamespace(
        id=300,
        content="<@777> yes",
        type=discord.MessageType.default,
        author=author,
        mentions=[me],
        channel=channel,
        guild=SimpleNamespace(id=700, name="test"),
        attachments=[],
        reference=None,
        message_snapshots=[],
        created_at=datetime.now(timezone.utc),
    )

    asyncio.run(adapter._dispatch_discord_message(msg))
    event = adapter.handle_message.call_args.args[0]

    # ASSERTION 1: Trusted bot input MUST have allow_gateway_control=False
    assert event.source.is_bot is True
    assert event.allow_gateway_control is False

    # ASSERTION 2: Plaintext approval resolver rejects bot event
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: adapter.config})
    runner.adapters = {Platform.DISCORD: adapter}
    runner.pairing_store = SimpleNamespace(is_approved=lambda *args: False)
    runner._pending_approvals = {}

    key = runner._session_key_for_source(event.source)
    entry = _ApprovalEntry({"command": "DANGEROUS_COMMAND_SENTINEL"})
    _gateway_queues.setdefault(key, []).append(entry)

    try:
        handled = asyncio.run(runner._route_plaintext_approval_while_busy(event, key))
        assert handled is False
        assert entry.event.is_set() is False
        assert entry.result is None

        # ASSERTION 3: Direct slash command /approve handler also rejects bot
        reply = asyncio.run(runner._handle_approve_command(event))
        assert entry.event.is_set() is False
        assert entry.result is None
    finally:
        _gateway_queues.clear()


def test_f4_yaml_allowed_bots_contract(tmp_path, monkeypatch):
    """F4: Test that allowed_bots in YAML (both platforms.discord.allowed_bots and
    platforms.discord.extra.allowed_bots) is recognized consistently by loader, adapter,
    and gateway _is_user_authorized under profile scope."""
    from gateway.config import load_gateway_config, Platform
    from gateway.authz_mixin import GatewayAuthorizationMixin
    from plugins.platforms.discord.adapter import DiscordAdapter

    config_file = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Variant A: platforms.discord.allowed_bots
    config_file.write_text("platforms:\n  discord:\n    allowed_bots: ['888']\n", encoding="utf-8")
    loaded_a = load_gateway_config()
    adapter_a = DiscordAdapter(loaded_a.platforms[Platform.DISCORD])
    runner_a = GatewayAuthorizationMixin()
    runner_a.config = loaded_a
    runner_a.adapters = {Platform.DISCORD: adapter_a}
    runner_a.pairing_store = SimpleNamespace(is_approved=lambda *args: False)

    src_a = SessionSource(platform=Platform.DISCORD, chat_id="100", user_id="888", is_bot=True)
    assert adapter_a._discord_allowed_bots() == {"888"}
    assert runner_a._is_user_authorized(src_a) is True

    # Variant B: platforms.discord.extra.allowed_bots
    config_file.write_text("platforms:\n  discord:\n    extra:\n      allowed_bots: ['888']\n", encoding="utf-8")
    loaded_b = load_gateway_config()
    adapter_b = DiscordAdapter(loaded_b.platforms[Platform.DISCORD])
    runner_b = GatewayAuthorizationMixin()
    runner_b.config = loaded_b
    runner_b.adapters = {Platform.DISCORD: adapter_b}
    runner_b.pairing_store = SimpleNamespace(is_approved=lambda *args: False)

    src_b = SessionSource(platform=Platform.DISCORD, chat_id="100", user_id="888", is_bot=True)
    assert adapter_b._discord_allowed_bots() == {"888"}
    assert runner_b._is_user_authorized(src_b) is True
