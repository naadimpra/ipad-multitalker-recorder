"""Connects one recording session to one AI adapter.

  AI iPad mic (float32, device rate, tagged with its recording frame)
      -> feed() -> resample to adapter.in_rate -> int16 -> adapter
  adapter audio (int16, adapter.out_rate)
      -> resample to the device rate -> float32 -> AI iPad, which plays it AND records it as channel 2

Everything the API says is logged to ai_events.jsonl; responses and what the AI heard are kept in
`self.responses` / `self.heard` and saved into session.json["ai"].
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np
import soxr

from ai_bridge.base import make_adapter


def now_ms() -> float:
    return time.time() * 1000.0


def pack(hdr: dict, payload: bytes) -> bytes:
    hb = json.dumps(hdr).encode()
    return struct.pack("<I", len(hb)) + hb + payload


class Bridge:
    def __init__(self, cfg: dict, instructions: str, dev_rate: int, sdir: Path,
                 send_bytes: Callable[[bytes], Awaitable[None]],
                 send_json: Callable[[dict], Awaitable[None]],
                 save: Callable[[], None]) -> None:
        self.adapter = make_adapter(cfg, instructions)
        self.dev_rate = int(dev_rate)
        self.send_bytes, self.send_json, self.save = send_bytes, send_json, save
        self.log_f = open(sdir / "ai_events.jsonl", "a")
        self.out_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=400)
        self.in_rs = soxr.ResampleStream(self.dev_rate, self.adapter.in_rate, 1, dtype="float32")
        self.out_rs = self._new_out_rs()
        self.out_turn: str | None = None
        self.in_samples = 0          # samples sent to the API (at in_rate)
        self.in_frame0: int | None = None   # AI iPad recording frame of the first sample sent
        self.muted = False
        self.closed = False
        self.responses: dict[str, dict] = {}
        self.heard: list[dict] = []
        self.speech: dict[str, dict] = {}   # item -> {frame_start, frame_end}
        self.status = {"state": "connecting", "last_ai": "", "last_heard": "", "error": "", "muted": False}
        self.tasks: list[asyncio.Task] = []

    def _new_out_rs(self):
        return soxr.ResampleStream(self.adapter.out_rate, self.dev_rate, 1, dtype="float32")

    async def _flush_out(self) -> None:
        """Push the resampler's last few ms out at the end of a turn, so the turn isn't clipped."""
        if self.out_turn is not None:
            y = self.out_rs.resample_chunk(np.zeros(0, np.float32), last=True)
            if len(y):
                await self.send_bytes(pack({"kind": "ai_out", "turn": self.out_turn}, y.astype("<f4").tobytes()))
        self.out_rs, self.out_turn = self._new_out_rs(), None

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.tasks.append(asyncio.create_task(self._run()))

    async def _run(self) -> None:
        try:
            await self.adapter.connect()
            self._log({"type": "connected", "adapter": self.adapter.describe()})
            self.status["state"] = "listening"
            self.tasks.append(asyncio.create_task(self._sender()))
            async for ev in self.adapter.events():
                await self._handle(ev)
            if not self.closed:
                self._fail("The AI service closed the connection.")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 — any failure must not stop the recording
            self._fail(f"{type(e).__name__}: {e}")

    def _fail(self, text: str) -> None:
        self.status.update(state="error", error=text)
        self._log({"type": "error", "text": text})
        print(f"  ! AI bridge: {text}")

    async def _sender(self) -> None:
        while True:
            pcm = await self.out_q.get()
            if pcm is None:
                return
            await self.adapter.send_audio(pcm)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.out_q.put_nowait(None)
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(self.adapter.close(), 3)
        except Exception:
            pass
        for t in self.tasks:
            t.cancel()
        if self.status["state"] != "error":
            self.status["state"] = "closed"
        self._log({"type": "closed"})
        self.log_f.close()

    # ---------- participant audio -> API ----------
    def feed(self, samples: np.ndarray, frame: int | None) -> None:
        if self.closed or self.status["state"] in ("connecting", "error"):
            return
        if self.in_frame0 is None and frame is not None:
            # samples already sent before this one (none, normally) are accounted for
            self.in_frame0 = int(frame) - round(self.in_samples * self.dev_rate / self.adapter.in_rate)
        x = np.zeros_like(samples) if self.muted else samples
        y = self.in_rs.resample_chunk(x.astype(np.float32))
        if not len(y):
            return
        self.in_samples += len(y)
        pcm = (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()
        try:
            self.out_q.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # API is too slow; dropping input is better than growing latency

    def _frame_of(self, audio_ms) -> int | None:
        if audio_ms is None or self.in_frame0 is None:
            return None
        return self.in_frame0 + round(audio_ms / 1000 * self.dev_rate)

    # ---------- API -> AI iPad ----------
    async def _handle(self, ev: dict) -> None:
        t = ev["type"]
        if t == "audio":
            turn = ev.get("turn") or "?"
            r = self.responses.setdefault(turn, {"turn": turn, "text": "", "first_audio_server_ms": now_ms(),
                                                 "done_server_ms": None})
            if r.get("first_audio_server_ms") is None:
                r["first_audio_server_ms"] = now_ms()
            self.status["state"] = "speaking"
            if self.out_turn not in (None, turn):
                await self._flush_out()
            self.out_turn = turn
            x = np.frombuffer(ev["pcm"], dtype="<i2").astype(np.float32) / 32768.0
            y = self.out_rs.resample_chunk(x)
            if len(y):
                await self.send_bytes(pack({"kind": "ai_out", "turn": turn}, y.astype("<f4").tobytes()))
            return
        rec = {k: v for k, v in ev.items() if k != "pcm"}
        self._log(rec)
        if t == "ready":
            self.status["state"] = "listening"
        elif t == "ai_text":
            turn = ev.get("turn") or "?"
            self.responses.setdefault(turn, {"turn": turn, "first_audio_server_ms": None, "done_server_ms": None})
            self.responses[turn]["text"] = ev["text"]
            self.status["last_ai"] = ev["text"]
            self.save()
        elif t == "heard_text":
            sp = self.speech.get(ev.get("item") or "", {})
            self.heard.append({"text": ev["text"], "server_ms": now_ms(), "item": ev.get("item"),
                               "frame_start": sp.get("frame_start"), "frame_end": sp.get("frame_end")})
            self.status["last_heard"] = ev["text"]
            self.save()
        elif t == "speech_started":
            if ev.get("item"):
                self.speech.setdefault(ev["item"], {})["frame_start"] = self._frame_of(ev.get("audio_ms"))
        elif t == "speech_stopped":
            if ev.get("item"):
                self.speech.setdefault(ev["item"], {})["frame_end"] = self._frame_of(ev.get("audio_ms"))
        elif t == "interrupted":
            self.out_rs, self.out_turn = self._new_out_rs(), None
            await self.send_json({"type": "ai_clear"})
        elif t == "turn_end":
            await self._flush_out()
            turn = ev.get("turn") or "?"
            if turn in self.responses:
                self.responses[turn]["done_server_ms"] = now_ms()
            self.status["state"] = "listening"
            self.save()
        elif t == "error":
            self.status["error"] = ev["text"]
            print(f"  ! AI: {ev['text']}")

    # ---------- admin controls ----------
    async def interrupt(self) -> None:
        self._log({"type": "admin_interrupt"})
        self.out_rs, self.out_turn = self._new_out_rs(), None
        await self.send_json({"type": "ai_clear"})
        try:
            await self.adapter.cancel()
        except Exception:
            pass

    def set_muted(self, on: bool) -> None:
        self.muted = on
        self.status["muted"] = on
        self._log({"type": "mute", "on": on})

    # ---------- saving ----------
    def _log(self, rec: dict) -> None:
        if self.log_f.closed:
            return
        self.log_f.write(json.dumps({"server_ms": round(now_ms(), 1), **rec}, ensure_ascii=False) + "\n")
        self.log_f.flush()

    def export(self) -> dict:
        return {"responses": list(self.responses.values()), "heard": self.heard,
                "input_frame0": self.in_frame0, "status": self.status["state"],
                "error": self.status["error"]}
