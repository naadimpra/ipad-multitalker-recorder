"""Record an AI agent running on this laptop (ChatGPT / Gemini / any app or web page) as the device "ai".

    pip install -r requirements-ai.txt
    python -m tools.loopback --list                         # show audio inputs and their channels
    python -m tools.loopback --device "AI Capture" --ai-ch 1,2 --mic-ch 3

It joins the running server like a recorder iPad, so the admin page shows it and it starts and stops with
every session. It records two channels:

  ch 1  "ai"   the agent's sound, taken digitally from a loopback device (e.g. BlackHole) — no room noise
  ch 2  "mic"  the laptop's microphone, which hears the sync chirp so tools.align can place the track

Use one macOS *Aggregate Device* (Audio MIDI Setup) that contains BlackHole and the built-in microphone,
so both channels come from one clock. See README → "AI agent in the conversation".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import ssl
import struct
import time
import uuid
from pathlib import Path

import numpy as np

STATE_FILE = Path(__file__).with_name(".loopback_id")


def now_ms() -> float:
    return time.time() * 1000.0


def list_devices() -> None:
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']!r}  inputs={d['max_input_channels']}  rate={d['default_samplerate']:.0f}")


def chans(s: str) -> list[int]:
    return [int(x) - 1 for x in s.split(",") if x.strip()]


class Loopback:
    def __init__(self, args) -> None:
        import sounddevice as sd
        self.sd = sd
        self.args = args
        info = sd.query_devices(args.device, "input")
        self.dev_name = info["name"]
        self.rate = int(args.rate or info["default_samplerate"])
        self.ai_ch = chans(args.ai_ch)
        self.mic_ch = chans(args.mic_ch) if args.mic_ch else []
        need = max(self.ai_ch + self.mic_ch) + 1
        if need > info["max_input_channels"]:
            raise SystemExit(f"{self.dev_name!r} has {info['max_input_channels']} input channels; "
                             f"you asked for channel {need}. Run with --list.")
        self.in_ch = need
        self.ch = 2 if self.mic_ch else 1
        self.id = STATE_FILE.read_text().strip() if STATE_FILE.exists() else f"loopback-{uuid.uuid4().hex[:8]}"
        STATE_FILE.write_text(self.id)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.blocks: asyncio.Queue = asyncio.Queue()
        self.ws = None
        self.rec: dict | None = None
        self.queue: dict[int, bytes] = {}
        self.seq = 0
        self.level = [-120.0, -120.0]   # ai, mic (dBFS rms over the last status period)
        self.peak = -120.0
        self.problem = ""

    # ---------- audio (sounddevice thread -> asyncio) ----------
    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.problem = str(status)
        ai = indata[:, self.ai_ch].mean(axis=1)
        cols = [ai] + ([indata[:, self.mic_ch].mean(axis=1)] if self.mic_ch else [])
        block = np.stack(cols, axis=1).astype(np.float32)
        t_first = now_ms() - frames / self.rate * 1000 - (self.stream.latency or 0) * 1000
        self.loop.call_soon_threadsafe(self.blocks.put_nowait, (block, t_first))

    # ---------- websocket ----------
    def describe(self) -> dict:
        return {"label": self.args.label, "speaker": "", "armed": True, "sample_rate": self.rate,
                "channels": self.ch, "mic": self.dev_name, "ua": "tools/loopback.py", "kind": "loopback",
                "channel_names": ["ai", "mic"] if self.mic_ch else ["ai"],
                "chirp_channel": 1 if self.mic_ch else None, "build": ""}

    async def send(self, obj) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(obj if isinstance(obj, bytes) else json.dumps(obj))
        except Exception:
            pass

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stream = self.sd.InputStream(device=self.args.device, channels=self.in_ch, samplerate=self.rate,
                                          dtype="float32", blocksize=2048, callback=self._callback)
        self.stream.start()
        print(f"Capturing {self.dev_name!r} at {self.rate} Hz: AI = input {self.args.ai_ch}"
              + (f", mic = input {self.args.mic_ch}" if self.mic_ch else " (no mic: alignment uses timestamps only)"))
        await asyncio.gather(self._audio(), self._connection(), self._status())

    async def _connection(self) -> None:
        from websockets.asyncio.client import connect
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # our own server on this laptop, mkcert certificate
        tls = {"ssl": ctx} if self.args.server.startswith("wss:") else {}
        warned = False
        while True:
            try:
                async with connect(self.args.server, max_size=None, **tls) as ws:
                    warned = False
                    self.ws = ws
                    await self.send({"type": "hello", "role": "device", "id": self.id, **self.describe()})
                    print(f"Connected to {self.args.server} as '{self.args.label}'")
                    for buf in list(self.queue.values()):
                        await self.send(buf)
                    await self._check_finished()
                    async for raw in ws:
                        if isinstance(raw, str):
                            await self._handle(json.loads(raw))
            except Exception as e:  # noqa: BLE001
                if self.ws is not None:
                    print(f"Disconnected ({type(e).__name__}); retrying…")
                elif not warned:
                    print(f"Cannot reach {self.args.server} ({type(e).__name__}: {e}). Is server.py running? Retrying…")
                    warned = True
            self.ws = None
            await asyncio.sleep(1)

    async def _handle(self, m: dict) -> None:
        t = m.get("type")
        if t == "ack":
            self.queue.pop(m.get("seq"), None)
            await self._check_finished()
        elif t == "start":
            self.rec = {"sid": m["sid"], "sent": 0, "pend": [], "pend_frames": 0, "t0": None,
                        "stopped": False, "finished": False}
            print(f"● Recording session {m['sid']}")
        elif t == "stop" and self.rec and self.rec["sid"] == m.get("sid"):
            await self._stop()
        elif t == "resume" and self.rec and self.rec["sid"] == m.get("sid"):
            if m.get("state") == "stopping" and not self.rec["stopped"]:
                await self._stop()
        elif t == "assign" and m.get("label"):
            self.args.label = m["label"]
        elif t == "saved":
            print(f"✓ Saved {m.get('file')} ({m.get('frames', 0) / self.rate:.1f} s)")

    async def _stop(self) -> None:
        self.rec["stopped"] = True
        await self._flush()
        await self._check_finished()

    async def _check_finished(self) -> None:
        r = self.rec
        if not r or not r["stopped"] or r["finished"] or self.queue or self.ws is None:
            return
        await self.send({"type": "finished", "sid": r["sid"], "frames": r["sent"], "events": []})
        r["finished"] = True

    async def _audio(self) -> None:
        acc_ai, acc_mic, n = 0.0, 0.0, 0
        while True:
            block, t_first = await self.blocks.get()
            acc_ai += float(np.mean(block[:, 0] ** 2))
            if self.ch > 1:
                acc_mic += float(np.mean(block[:, 1] ** 2))
            self.peak = max(self.peak, 20 * math.log10(float(np.abs(block).max()) + 1e-12))
            n += 1
            if n >= 5:
                self.level = [10 * math.log10(acc_ai / n + 1e-12), 10 * math.log10(acc_mic / n + 1e-12)]
                acc_ai, acc_mic, n = 0.0, 0.0, 0
            r = self.rec
            if r and not r["stopped"]:
                if r["sent"] == 0 and r["pend_frames"] == 0:
                    r["t0"] = t_first
                r["pend"].append(block)
                r["pend_frames"] += len(block)
                if r["pend_frames"] >= self.rate:
                    await self._flush()

    async def _flush(self) -> None:
        r = self.rec
        if not r or not r["pend_frames"]:
            return
        pcm = np.concatenate(r["pend"]).astype("<f4")
        hdr = {"sid": r["sid"], "seq": self.seq, "start": r["sent"], "frames": len(pcm), "ch": self.ch}
        if r["sent"] == 0:
            hdr["t0_server_ms"] = r["t0"]
        hb = json.dumps(hdr).encode()
        buf = struct.pack("<I", len(hb)) + hb + pcm.tobytes()
        self.queue[self.seq] = buf
        self.seq += 1
        r["sent"] += len(pcm)
        r["pend"], r["pend_frames"] = [], 0
        await self.send(buf)

    async def _status(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            r = self.rec
            recording = bool(r and not r["stopped"])
            backlog = sum(len(b) for b in self.queue.values()) / (4 * self.ch * self.rate)
            # The meter shows the mic (it must hear the chirp); "AI speaking" shows the loopback channel.
            lvl = self.level[1] if self.ch > 1 else self.level[0]
            await self.send({"type": "status", **self.describe(), "ctx": "running", "hidden": False,
                             "level_db": lvl, "peak_db": self.peak, "rtt_ms": 0, "offset_ms": 0,
                             "backlog_s": backlog, "quiet": False, "recording": recording,
                             "sid": r["sid"] if r else None, "problem": self.problem,
                             "ai_speaking": recording and self.level[0] > -50})
            self.peak = -120.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list audio inputs and exit")
    ap.add_argument("--device", help="input device name or index (an Aggregate Device with BlackHole + mic)")
    ap.add_argument("--ai-ch", default="1,2", help="1-based input channels carrying the AI's sound (mixed to mono)")
    ap.add_argument("--mic-ch", default="", help="1-based input channel of the microphone that hears the chirp")
    ap.add_argument("--rate", type=int, default=0, help="sample rate (default: the device's)")
    ap.add_argument("--label", default="ai", help="device name (default: ai)")
    ap.add_argument("--server", default="wss://127.0.0.1:8443/ws")
    args = ap.parse_args()
    if args.list:
        list_devices()
        return
    if args.device is None:
        raise SystemExit("Pass --device (see --list).")
    if args.device.isdigit():
        args.device = int(args.device)
    try:
        asyncio.run(Loopback(args).run())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
