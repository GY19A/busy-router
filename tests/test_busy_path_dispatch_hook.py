"""busy-router gateway patch: pre_gateway_dispatch must also see messages that arrive while a turn is running.

Drives the real adapter busy path (BasePlatformAdapter._handle_message_while_active ->
runner busy handler -> plugin hook) with a stub hook, and checks the three hook outcomes.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.MATRIX)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SimpleNamespace(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _event(text="how far along are you"):
    src = SessionSource(platform=Platform.MATRIX, chat_id="!r:hs", chat_type="dm", user_id="@admin:hs", user_name="admin")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=src, message_id="e1")


def _runner(adapter):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.MATRIX: adapter}
    runner._profile_adapters = {}
    runner._draining = False
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._effective_busy_input_mode = lambda source: "interrupt"
    runner._effective_busy_text_mode = lambda source: "interrupt"
    runner._delivery_adapter_for = lambda source: adapter
    runner._queue_or_replace_pending_event = MagicMock()
    agent = MagicMock()
    runner._peek_session_state = lambda key: SimpleNamespace(turn=SimpleNamespace(agent=agent, busy_ack_ts=0))
    runner._resolve_busy_steer_or_redirect = AsyncMock(return_value=SimpleNamespace(
        effective_mode="interrupt", redirected=False, steered=False,
        demoted_for_subagents=False, demoted_for_compression=False))
    runner._interrupt_running_agent_for_busy_event = AsyncMock()
    runner._session_state = lambda key: SimpleNamespace(turn=SimpleNamespace(busy_ack_ts=0))
    runner._compose_busy_ack_message = lambda *a, **k: "busy"
    runner._send_busy_ack_reply = AsyncMock()
    return runner, agent


def _wire(monkeypatch, hook_results):
    import hermes_cli.lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "ainvoke_hook", AsyncMock(return_value=hook_results))
    adapter = _Adapter()
    runner, agent = _runner(adapter)
    handled_events = []

    async def _message_handler(ev):
        handled_events.append(ev)
        return f"ran {ev.text}"

    adapter.set_message_handler(_message_handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    ev = _event()
    key = build_session_key(ev.source)
    adapter._active_sessions[key] = asyncio.Event()
    return adapter, runner, agent, handled_events, ev, key


@pytest.mark.asyncio
async def test_busy_message_rewritten_to_command_is_dispatched_inline(monkeypatch):
    adapter, runner, agent, handled, ev, key = _wire(monkeypatch, [{"action": "rewrite", "text": "/btw how far along are you"}])
    await adapter._handle_message_while_active(ev, key)
    assert [e.text for e in handled] == ["/btw how far along are you"]
    assert adapter.sent == ["ran /btw how far along are you"]
    runner._interrupt_running_agent_for_busy_event.assert_not_awaited()
    runner._queue_or_replace_pending_event.assert_not_called()
    assert key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_busy_message_without_hook_result_keeps_busy_mode(monkeypatch):
    adapter, runner, agent, handled, ev, key = _wire(monkeypatch, [])
    await adapter._handle_message_while_active(ev, key)
    assert handled == []
    runner._interrupt_running_agent_for_busy_event.assert_awaited_once()
    runner._queue_or_replace_pending_event.assert_called_once()


@pytest.mark.asyncio
async def test_busy_message_skipped_by_hook_is_dropped(monkeypatch):
    adapter, runner, agent, handled, ev, key = _wire(monkeypatch, [{"action": "skip", "reason": "x"}])
    await adapter._handle_message_while_active(ev, key)
    assert handled == [] and adapter.sent == []
    runner._interrupt_running_agent_for_busy_event.assert_not_awaited()
    runner._queue_or_replace_pending_event.assert_not_called()


@pytest.mark.asyncio
async def test_busy_slash_command_does_not_reach_hook(monkeypatch):
    adapter, runner, agent, handled, ev, key = _wire(monkeypatch, [{"action": "rewrite", "text": "/bg nope"}])
    import hermes_cli.lifecycle as lifecycle
    ev = _event("/status")
    await adapter._handle_message_while_active(ev, key)
    lifecycle.ainvoke_hook.assert_not_awaited()
