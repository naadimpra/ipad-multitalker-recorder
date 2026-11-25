"""Google Gemini Live API. Needs GEMINI_API_KEY (from Google AI Studio).

Docs: https://ai.google.dev/gemini-api/docs/live
"""
from __future__ import annotations

import base64
import json
from typing import AsyncIterator

from ai_bridge.base import Adapter, as_json, ws_connect

URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


class GeminiLive(Adapter):
    in_rate = 16000
    out_rate = 24000

    async def connect(self) -> None:
        if not self.key:
            raise RuntimeError(f"Set {self.cfg.get('key_env', 'GEMINI_API_KEY')} before starting the server.")
        self.ws = await ws_connect(URL, {"x-goog-api-key": self.key})
        model = self.cfg.get("model", "gemini-2.5-flash-native-audio-preview-09-2025")
        setup = {
            "model": model if model.startswith("models/") else f"models/{model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.cfg.get("voice", "Puck")}}},
            },
            "systemInstruction": {"parts": [{"text": self.instructions}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
        await self.ws.send(json.dumps({"setup": setup}))
        self.turn_n = 1
        self.turn_open = False
        self.ai_text: list[str] = []
        self.heard: list[str] = []

    async def send_audio(self, pcm16: bytes) -> None:
        await self.ws.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm16).decode(), "mimeType": f"audio/pcm;rate={self.in_rate}"}}}))

    async def close(self) -> None:
        await self.ws.close()

    def _turn(self) -> str:
        return f"g{self.turn_n}"

    def _flush_heard(self):
        text = "".join(self.heard).strip()
        self.heard = []
        return [{"type": "heard_text", "text": text, "item": None}] if text else []

    def _end_turn(self):
        out = []
        if self.turn_open:
            text = "".join(self.ai_text).strip()
            if text:
                out.append({"type": "ai_text", "text": text, "turn": self._turn()})
            out.append({"type": "turn_end", "turn": self._turn()})
            self.turn_n += 1
        self.turn_open = False
        self.ai_text = []
        return out

    async def events(self) -> AsyncIterator[dict]:
        async for raw in self.ws:
            m = as_json(raw)
            if "setupComplete" in m:
                yield {"type": "ready"}
            if "goAway" in m:
                yield {"type": "error", "text": f"Gemini will close the connection soon: {m['goAway']}"}
            sc = m.get("serverContent")
            if not sc:
                continue
            if sc.get("inputTranscription", {}).get("text"):
                self.heard.append(sc["inputTranscription"]["text"])
            for part in (sc.get("modelTurn") or {}).get("parts", []):
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    if not self.turn_open:
                        for ev in self._flush_heard():   # the participants' turn is over once the AI answers
                            yield ev
                        self.turn_open = True
                    yield {"type": "audio", "pcm": base64.b64decode(data), "turn": self._turn()}
            if sc.get("outputTranscription", {}).get("text"):
                self.ai_text.append(sc["outputTranscription"]["text"])
            if sc.get("interrupted"):
                yield {"type": "interrupted"}
                for ev in self._end_turn():
                    yield ev
            if sc.get("turnComplete"):
                for ev in self._end_turn() + self._flush_heard():
                    yield ev
