"""Shared WebUI metadata keys."""

WEBUI_TURN_METADATA_KEY = "webui_turn_id"
WEBUI_SYSTEM_COMMAND_TURN_PREFIX = "webui-system:"
WEBSOCKET_TURN_OWNER_METADATA_KEY = "_websocket_turn_owner"
WEBUI_MESSAGE_SOURCE_METADATA_KEY = "_webui_message_source"
# Client asked for this turn's final reply as a voice message (``voice_reply`` on the ``message`` envelope).
WEBUI_VOICE_REPLY_METADATA_KEY = "voice_reply"
# Per-turn opt-out of token streaming: the final answer is delivered as one message (needed to attach audio).
SUPPRESS_STREAM_METADATA_KEY = "_suppress_stream"
