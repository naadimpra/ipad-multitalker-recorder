"""Interface every AI agent adapter implements.

An adapter connects to one realtime voice API, takes the participants' audio (16-bit PCM, mono, at
`in_rate`) and yields events as plain dicts:

  {"type": "ready"}
  {"type": "audio", "pcm": bytes, "turn": str}            int16 mono at `out_rate`, part of AI turn `turn`
  {"type": "ai_text", "text": str, "turn": str}           final transcript of what the AI said in `turn`
  {"type": "heard_text", "text": str, "item": str|None}   what the API heard the participants say
  {"type": "speech_started", "audio_ms": int|None, "item": str|None}   participant speech began (VAD)
  {"type": "speech_stopped", "audio_ms": int|None, "item": str|None}
  {"type": "interrupted"}                                 the AI stopped talking because someone talked over it
  {"type": "turn_end", "turn": str}
  {"type": "error", "text": str}

`audio_ms` counts milliseconds of input audio sent since the connection opened, so the bridge can map it
back to a frame of the AI iPad's recording.

To add another agent, write a module with a subclass of Adapter and register it in ADAPTERS below.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator


class Adapter:
    in_rate = 24000
    out_rate = 24000

    def __init__(self, cfg: dict, instructions: str) -> None:
        self.cfg = cfg
        self.instructions = instructions
        self.key = os.environ.get(cfg.get("key_env", ""), "")

    async def connect(self) -> None:
        raise NotImplementedError

    async def send_audio(self, pcm16: bytes) -> None:
        raise NotImplementedError

    async def cancel(self) -> None:
        """Stop the current AI response, if the API supports it."""

    async def close(self) -> None:
        raise NotImplementedError

    def events(self) -> AsyncIterator[dict]:
        raise NotImplementedError

    def describe(self) -> dict:
        """Saved into session.json so the session can be reproduced."""
        return {k: v for k, v in self.cfg.items() if k != "key_env"} | {"in_rate": self.in_rate,
                                                                         "out_rate": self.out_rate}


async def ws_connect(url: str, headers: dict | None = None):
    from websockets.asyncio.client import connect
    return await connect(url, additional_headers=headers or {}, max_size=None, ping_interval=20)


def as_json(msg) -> dict:
    return json.loads(msg.decode() if isinstance(msg, (bytes, bytearray)) else msg)


def make_adapter(cfg: dict, instructions: str) -> Adapter:
    kind = cfg.get("adapter")
    if kind == "openai_realtime":
        from ai_bridge.openai_realtime import OpenAIRealtime
        return OpenAIRealtime(cfg, instructions)
    if kind == "gemini_live":
        from ai_bridge.gemini_live import GeminiLive
        return GeminiLive(cfg, instructions)
    raise ValueError(f"Unknown adapter '{kind}' (known: openai_realtime, gemini_live)")


ADAPTERS = ("openai_realtime", "gemini_live")
