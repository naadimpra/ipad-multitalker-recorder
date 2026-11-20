"""OpenAI Realtime API (the voice behind ChatGPT's voice mode). Needs OPENAI_API_KEY.

Docs: https://platform.openai.com/docs/guides/realtime  — event names differ slightly between the beta and
the GA API, so both spellings are accepted below.
"""
from __future__ import annotations

import base64
import json
from typing import AsyncIterator

from ai_bridge.base import Adapter, as_json, ws_connect


class OpenAIRealtime(Adapter):
    in_rate = 24000
    out_rate = 24000

    async def connect(self) -> None:
        if not self.key:
            raise RuntimeError(f"Set {self.cfg.get('key_env', 'OPENAI_API_KEY')} before starting the server.")
        model = self.cfg.get("model", "gpt-realtime")
        self.ws = await ws_connect(f"wss://api.openai.com/v1/realtime?model={model}",
                                   {"Authorization": f"Bearer {self.key}"})
        audio_in = {"format": {"type": "audio/pcm", "rate": self.in_rate},
                    "turn_detection": self.cfg.get("turn_detection", {"type": "server_vad"})}
        if self.cfg.get("transcribe_model"):
            audio_in["transcription"] = {"model": self.cfg["transcribe_model"]}
        await self.ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": model, "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {"input": audio_in,
                      "output": {"format": {"type": "audio/pcm", "rate": self.out_rate},
                                 "voice": self.cfg.get("voice", "marin")}},
        }}))

    async def send_audio(self, pcm16: bytes) -> None:
        await self.ws.send(json.dumps({"type": "input_audio_buffer.append",
                                       "audio": base64.b64encode(pcm16).decode()}))

    async def cancel(self) -> None:
        await self.ws.send(json.dumps({"type": "response.cancel"}))

    async def close(self) -> None:
        await self.ws.close()

    async def events(self) -> AsyncIterator[dict]:
        async for raw in self.ws:
            m = as_json(raw)
            t = m.get("type", "")
            if t == "session.updated":
                yield {"type": "ready"}
            elif t in ("response.output_audio.delta", "response.audio.delta"):
                yield {"type": "audio", "pcm": base64.b64decode(m["delta"]), "turn": m.get("response_id", "")}
            elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                yield {"type": "ai_text", "text": m.get("transcript", ""), "turn": m.get("response_id", "")}
            elif t == "conversation.item.input_audio_transcription.completed":
                yield {"type": "heard_text", "text": m.get("transcript", ""), "item": m.get("item_id")}
            elif t == "input_audio_buffer.speech_started":
                yield {"type": "speech_started", "audio_ms": m.get("audio_start_ms"), "item": m.get("item_id")}
            elif t == "input_audio_buffer.speech_stopped":
                yield {"type": "speech_stopped", "audio_ms": m.get("audio_end_ms"), "item": m.get("item_id")}
            elif t == "response.done":
                r = m.get("response", {})
                if r.get("status") == "cancelled":
                    yield {"type": "interrupted"}
                yield {"type": "turn_end", "turn": r.get("id", "")}
            elif t == "error":
                err = m.get("error", {})
                yield {"type": "error", "text": err.get("message") or json.dumps(err)}
