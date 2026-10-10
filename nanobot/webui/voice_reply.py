"""Optional server-side spoken replies for the WebSocket channel.

A client may set ``voice_reply: true`` on a ``message`` envelope. For the FINAL ordinary assistant message of that
turn the gateway then synthesizes speech and attaches the audio file to the message, so it is delivered (and persisted
in the session transcript) like any other media attachment.

Speech comes from the optional ``homelab_tools.voice`` package (installed in the same environment by the homelab
deployment). Nothing here is required: when the package, its credentials or the daily cap are unavailable the text
message is sent normally with a short ``voice_error`` string for the client to show.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.webui.metadata import WEBUI_VOICE_REPLY_METADATA_KEY

#: Seconds to wait for synthesis before falling back to text.
VOICE_TIMEOUT_S = 12.0
#: Spoken text is cut to about this many characters at a sentence end.
SPEAK_CHARS = 1200

VOICE_NOT_SET_UP = "voice not set up"
VOICE_CAP = "daily voice limit reached"
VOICE_TIMEOUT = "voice took too long"
VOICE_FAILED = "voice failed"

_ERROR_REPLY_PREFIXES = ("Sorry, I encountered an error",)
_APPROVAL_PREFIX = "Pending approval ["


def requested(metadata: Mapping[str, Any] | None) -> bool:
    return bool(metadata) and metadata.get(WEBUI_VOICE_REPLY_METADATA_KEY) is True  # type: ignore[union-attr]


def eligible(text: str, metadata: Mapping[str, Any] | None, *, is_progress: bool) -> bool:
    """True for an ordinary final assistant message of a ``voice_reply`` turn."""
    if not requested(metadata) or is_progress:
        return False
    body = (text or "").strip()
    if not body:
        return False
    if body.startswith(_APPROVAL_PREFIX) or _APPROVAL_PREFIX in body.split("\n", 1)[0]:
        return False
    if body.startswith(_ERROR_REPLY_PREFIXES):
        return False
    md = metadata or {}
    if md.get("_stop_reason") in {"error", "tool_error"}:
        return False
    if md.get("render_as") == "text":  # slash-command style replies
        return False
    return True


def trim_for_speech(spoken: str, limit: int = SPEAK_CHARS) -> str:
    """First ~limit characters, cut at a sentence end (or a word) rather than mid-word."""
    spoken = spoken.strip()
    if len(spoken) <= limit:
        return spoken
    head = spoken[:limit]
    cut = max((m.end() for m in re.finditer(r"[.!?](?=\s)", head)), default=-1)
    if cut < limit * 0.6:
        cut = head.rfind(" ")
    if cut <= 0:
        cut = limit
    return head[:cut].strip()


async def synthesize(text: str, *, timeout: float = VOICE_TIMEOUT_S) -> tuple[Path | None, str | None]:
    """Return ``(audio_path, None)`` on success or ``(None, error)`` with a short client-facing reason.

    Never raises and never blocks the event loop: the blocking work (usage file, HTTP) runs in worker threads.
    """
    try:
        from homelab_tools.voice import tts, ttsd
    except Exception:  # ImportError, or the package failing to import its own config
        return None, VOICE_NOT_SET_UP
    try:
        spoken = trim_for_speech(ttsd.speakable(text))
        if not spoken:
            return None, None
        if not await asyncio.to_thread(ttsd._spend, len(spoken)):  # noqa: SLF001 - the daily cap lives there
            return None, VOICE_CAP
        path = await asyncio.wait_for(asyncio.to_thread(tts.speak, spoken), timeout=timeout)
        audio = Path(path)
        if not audio.is_file():
            return None, VOICE_FAILED
        return audio, None
    except TimeoutError:
        return None, VOICE_TIMEOUT
    except Exception as exc:  # NotConfigured, provider errors, disk problems
        if type(exc).__name__ == "NotConfigured":
            return None, VOICE_NOT_SET_UP
        logger.warning("voice reply synthesis failed: {}", type(exc).__name__)
        return None, VOICE_FAILED
