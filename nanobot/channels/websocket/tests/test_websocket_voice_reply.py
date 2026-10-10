"""``voice_reply`` on the WebSocket ``message`` envelope: server-synthesized spoken replies."""

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.asyncio.client import connect

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session.manager import SessionManager
from nanobot.webui import voice_reply
from nanobot.webui.gateway_services import build_gateway_services

APPROVAL = "Pending approval [k7mq]: Restart the Plex container\nNothing has been done yet."


class NotConfigured(Exception):
    """Same name as homelab_tools.common.secrets.NotConfigured (matched by class name)."""


def install_fake_voice(monkeypatch, tmp_path, *, cap_ok=True, speak=None, speakable=None):
    """Put a fake ``homelab_tools.voice`` package (tts + ttsd) on sys.modules. No network, no real package."""
    calls = {"speak": [], "spend": []}
    mp3 = tmp_path / "speech-test.mp3"
    mp3.write_bytes(b"ID3fake-mp3")

    def default_speak(text):
        calls["speak"].append(text)
        return mp3

    tts = types.ModuleType("homelab_tools.voice.tts")
    tts.speak = speak or default_speak
    ttsd = types.ModuleType("homelab_tools.voice.ttsd")
    ttsd.speakable = speakable or (lambda t: t.replace("**", "").strip())
    ttsd.MAX_REQUEST_CHARS = 1500

    def spend(n):
        calls["spend"].append(n)
        return cap_ok

    ttsd._spend = spend
    pkg = types.ModuleType("homelab_tools")
    voice = types.ModuleType("homelab_tools.voice")
    voice.tts, voice.ttsd = tts, ttsd
    for name, mod in {
        "homelab_tools": pkg,
        "homelab_tools.voice": voice,
        "homelab_tools.voice.tts": tts,
        "homelab_tools.voice.ttsd": ttsd,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls, mp3


def make_channel(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    ws_media = media_root / "websocket"
    ws_media.mkdir(parents=True)
    monkeypatch.setattr(
        "nanobot.webui.media_gateway.get_media_dir",
        lambda channel=None: ws_media if channel == "websocket" else media_root,
    )
    bus = MagicMock()
    cfg = WebSocketConfig.model_validate({"enabled": True, "allowFrom": ["*"], "websocketRequiresToken": False})
    services = build_gateway_services(
        config=cfg, bus=bus, session_manager=None, static_dist_path=None, workspace_path=tmp_path,
        default_restrict_to_workspace=False, runtime_model_name=None, runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus, gateway=services)
    ws = AsyncMock()
    channel._attach(ws, "chat-1")
    return channel, ws, ws_media


def sent(ws):
    return json.loads(ws.send.call_args[0][0])


def final(text, **meta):
    return OutboundMessage(channel="websocket", chat_id="chat-1", content=text, metadata={"voice_reply": True, **meta})


# ---- pure helpers ----

def test_eligible_matrix():
    md = {"voice_reply": True}
    assert voice_reply.eligible("The lights are off.", md, is_progress=False)
    assert not voice_reply.eligible("The lights are off.", {}, is_progress=False)  # flag absent
    assert not voice_reply.eligible("reading file", md, is_progress=True)  # progress / tool hint
    assert not voice_reply.eligible("   ", md, is_progress=False)  # empty
    assert not voice_reply.eligible(APPROVAL, md, is_progress=False)  # approval card text
    assert not voice_reply.eligible("Sorry, I encountered an error.", md, is_progress=False)
    assert not voice_reply.eligible("boom", {**md, "_stop_reason": "error"}, is_progress=False)
    assert not voice_reply.eligible("[k7mq] denied. Nothing was done.", {**md, "render_as": "text"}, is_progress=False)


def test_trim_for_speech_cuts_at_sentence():
    long = "This is a sentence about the house. " * 60
    out = voice_reply.trim_for_speech(long)
    assert len(out) <= voice_reply.SPEAK_CHARS and out.endswith(".")
    assert voice_reply.trim_for_speech("short text") == "short text"


# ---- synthesize ----

async def test_synthesize_success_uses_speakable_cap_and_thread(monkeypatch, tmp_path):
    calls, mp3 = install_fake_voice(monkeypatch, tmp_path)
    path, err = await voice_reply.synthesize("**Hello** there")
    assert (path, err) == (mp3, None)
    assert calls["speak"] == ["Hello there"]  # markdown stripped by ttsd.speakable
    assert calls["spend"] == [len("Hello there")]  # cap charged with the spoken length


async def test_synthesize_respects_daily_cap(monkeypatch, tmp_path):
    calls, _ = install_fake_voice(monkeypatch, tmp_path, cap_ok=False)
    path, err = await voice_reply.synthesize("Hello there")
    assert path is None and err == "daily voice limit reached"
    assert calls["speak"] == []  # never spent a provider call


async def test_synthesize_without_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "homelab_tools", None)  # makes the import raise ImportError
    path, err = await voice_reply.synthesize("Hello")
    assert (path, err) == (None, "voice not set up")


async def test_synthesize_not_configured_and_provider_error(monkeypatch, tmp_path):
    def not_configured(_t):
        raise NotConfigured("elevenlabs_api_key")

    install_fake_voice(monkeypatch, tmp_path, speak=not_configured)
    assert await voice_reply.synthesize("Hello") == (None, "voice not set up")

    def boom(_t):
        raise RuntimeError("ElevenLabs returned 500")

    install_fake_voice(monkeypatch, tmp_path, speak=boom)
    assert await voice_reply.synthesize("Hello") == (None, "voice failed")


async def test_synthesize_times_out_without_blocking_the_loop(monkeypatch, tmp_path):
    import threading

    gate = threading.Event()

    def slow(_t):
        gate.wait(3)
        return tmp_path / "late.mp3"

    install_fake_voice(monkeypatch, tmp_path, speak=slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        path, err = await voice_reply.synthesize("Hello", timeout=0.2)
    finally:
        gate.set()
        t.cancel()
    assert (path, err) == (None, "voice took too long")
    assert ticks >= 5  # the event loop kept running while synthesis was pending


# ---- outbound delivery through the channel ----

async def test_voice_turn_attaches_audio_and_keeps_text(monkeypatch, tmp_path):
    install_fake_voice(monkeypatch, tmp_path)
    channel, ws, ws_media = make_channel(tmp_path, monkeypatch)
    await channel.send(final("The garage lights are off.", webui_turn_id="t1"))
    payload = sent(ws)
    assert payload["event"] == "message"
    assert payload["text"] == "The garage lights are off."  # written reply stays available
    assert payload["turn_id"] == "t1"
    assert "voice_error" not in payload
    assert payload["media"][0].endswith("speech-test.mp3")
    url = payload["media_urls"][0]
    assert url["url"].startswith("/api/media/") and url["name"].endswith(".mp3")
    assert any(p.name.endswith("speech-test.mp3") for p in ws_media.iterdir())  # staged into the media dir


@pytest.mark.parametrize(
    "text",
    [APPROVAL, "Sorry, I encountered an error.", "   "],
)
async def test_no_audio_for_approval_error_or_empty(monkeypatch, tmp_path, text):
    calls, _ = install_fake_voice(monkeypatch, tmp_path)
    channel, ws, _ = make_channel(tmp_path, monkeypatch)
    await channel.send(final(text))
    payload = sent(ws)
    assert "media_urls" not in payload and "voice_error" not in payload
    assert calls["speak"] == []


async def test_no_audio_for_progress_breadcrumbs(monkeypatch, tmp_path):
    calls, _ = install_fake_voice(monkeypatch, tmp_path)
    channel, ws, _ = make_channel(tmp_path, monkeypatch)
    await channel.send(
        OutboundMessage(
            channel="websocket", chat_id="chat-1", content="Reading the calendar",
            metadata={"voice_reply": True}, event=ProgressEvent(tool_hint=True),
        )
    )
    payload = sent(ws)
    assert payload["kind"] == "tool_hint"
    assert "media_urls" not in payload and calls["speak"] == []


async def test_failure_sends_text_with_voice_error(monkeypatch, tmp_path):
    def boom(_t):
        raise RuntimeError("provider down")

    install_fake_voice(monkeypatch, tmp_path, speak=boom)
    channel, ws, _ = make_channel(tmp_path, monkeypatch)
    await channel.send(final("Hello there"))
    payload = sent(ws)
    assert payload["text"] == "Hello there"
    assert payload["voice_error"] == "voice failed"
    assert "media_urls" not in payload


async def test_cap_reached_sets_voice_error(monkeypatch, tmp_path):
    calls, _ = install_fake_voice(monkeypatch, tmp_path, cap_ok=False)
    channel, ws, _ = make_channel(tmp_path, monkeypatch)
    await channel.send(final("Hello there"))
    payload = sent(ws)
    assert payload["voice_error"] == "daily voice limit reached"
    assert "media_urls" not in payload and calls["speak"] == []


async def test_missing_package_sets_voice_not_set_up(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "homelab_tools", None)
    channel, ws, _ = make_channel(tmp_path, monkeypatch)
    await channel.send(final("Hello there"))
    assert sent(ws)["voice_error"] == "voice not set up"


async def test_normal_turn_is_untouched(monkeypatch, tmp_path):
    calls, _ = install_fake_voice(monkeypatch, tmp_path)
    channel, ws, _ = make_channel(tmp_path, monkeypatch)
    await channel.send(OutboundMessage(channel="websocket", chat_id="chat-1", content="Hello there"))
    payload = sent(ws)
    assert payload == {"event": "message", "chat_id": "chat-1", "text": "Hello there"}
    assert calls["speak"] == [] and calls["spend"] == []


# ---- inbound flag parsing over a real listener ----

@pytest.fixture
async def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    bus = MessageBus()
    config = WebSocketConfig(port=0, path="/ws", token="test-secret")
    services = build_gateway_services(
        config=config, bus=bus, session_manager=SessionManager(tmp_path), static_dist_path=None,
        workspace_path=tmp_path, default_restrict_to_workspace=True, runtime_model_name=None,
        runtime_surface="browser", runtime_capabilities_overrides=None, config_path=tmp_path / "config.json",
    )
    channel = WebSocketChannel(config, bus, gateway=services)
    task = asyncio.create_task(channel.start())
    try:
        async with asyncio.timeout(10):
            while channel._server is None:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        port = channel._server.sockets[0].getsockname()[1]
        yield channel, bus, f"ws://127.0.0.1:{port}/ws?token=test-secret"
    finally:
        await channel.stop()
        await task


async def _event(ws, name):
    async with asyncio.timeout(5):
        while True:
            payload = json.loads(await ws.recv())
            if payload["event"] == name:
                return payload


async def _send_and_consume(gateway, **extra):
    _, bus, url = gateway
    async with connect(url) as ws:
        ready = await _event(ws, "ready")
        frame = {"type": "message", "chat_id": ready["chat_id"], "webui": True, "turn_id": "turn-1",
                 "content": "tell me about the house", **extra}
        await ws.send(json.dumps(frame))
        await _event(ws, "message_accepted")
        return await asyncio.wait_for(bus.consume_inbound(), 2)


async def test_voice_reply_flag_reaches_inbound_metadata_and_disables_streaming(gateway):
    inbound = await _send_and_consume(gateway, voice_reply=True)
    assert inbound.metadata["voice_reply"] is True
    assert inbound.metadata.get("_wants_stream") is not True  # one final message so audio can be attached


async def test_without_flag_turn_streams_as_before(gateway):
    inbound = await _send_and_consume(gateway)
    assert "voice_reply" not in inbound.metadata
    assert inbound.metadata.get("_wants_stream") is True


async def test_flag_must_be_boolean_true_and_ignored_for_commands(gateway):
    inbound = await _send_and_consume(gateway, voice_reply="yes")
    assert "voice_reply" not in inbound.metadata
    cmd = await _send_and_consume(gateway, voice_reply=True, content="/status")
    assert "voice_reply" not in cmd.metadata
