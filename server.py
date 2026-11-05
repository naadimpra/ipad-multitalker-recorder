"""Multi-iPad synchronized recording server.

Run:  python server.py            (HTTPS on :8443, CA-download helper on :8080)
iPads open   https://<laptop-ip>:8443/record
Admin opens  https://<laptop-ip>:8443/control
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from tools.common import raw_to_wav

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
REC_DIR = ROOT / "recordings"
CERT_DIR = ROOT / "certs"

app = FastAPI()

# --- version stamps, so stale pages / a stale server are visible instead of silently misbehaving ---
_build_cache: tuple[float, str] = (0.0, "")


def static_build() -> str:
    """Short hash of everything in static/ (names, sizes, mtimes); cached for 1 s."""
    global _build_cache
    if time.time() - _build_cache[0] < 1.0:
        return _build_cache[1]
    h = hashlib.sha1()
    for f in sorted(STATIC.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.relative_to(STATIC)}:{st.st_size}:{st.st_mtime_ns}".encode())
    _build_cache = (time.time(), h.hexdigest()[:8])
    return _build_cache[1]


def server_src_hash() -> str:
    return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:8]


SERVER_BOOT_HASH = server_src_hash()  # server.py as it was when this process started


def stamped_js(name: str):
    """Serve a page script with its build id baked in, so each page can report which version it runs."""
    text = (STATIC / name).read_text().replace("__BUILD__", static_build())
    return Response(text, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/static/record.js")
def record_js():
    return stamped_js("record.js")


@app.get("/static/control.js")
def control_js():
    return stamped_js("control.js")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def now_ms() -> float:
    return time.time() * 1000.0


class Hub:
    def __init__(self) -> None:
        self.devices: dict[str, dict] = {}      # device_id -> info (incl. "ws")
        self.admins: set[WebSocket] = set()
        self.session: dict | None = None       # active session
        self.last_session: str | None = None
        self.rotate = False        # "mics toward the participant" guide for speaker iPads
        self.rotate_epoch = 0      # bumps each time it is switched on, so devices ask again
        self.lock = asyncio.Lock()

    # ---------- state ----------
    def public_devices(self) -> list[dict]:
        out = []
        for did, d in self.devices.items():
            out.append({k: v for k, v in d.items() if k not in ("ws",)} | {"id": did})
        return sorted(out, key=lambda d: d.get("label", ""))

    def state(self) -> dict:
        s = self.session
        sess = None
        if s:
            sess = {
                "id": s["meta"]["session_id"], "name": s["meta"]["name"], "mode": s["meta"]["mode"],
                "state": s["state"], "started_at": s["meta"]["started_at_ms"],
                "finished": sorted(s["finished"]), "members": sorted(s["members"].values()),
                "markers": len(s["meta"]["markers"]),
            }
        return {"type": "state", "server_ms": now_ms(), "devices": self.public_devices(),
                "session": sess, "last_session": self.last_session,
                "rotate": self.rotate, "build": static_build(), "update": UPDATE if UPDATE["available"] else None, "server_stale": server_src_hash() != SERVER_BOOT_HASH}

    async def to_admins(self, msg: dict) -> None:
        dead = []
        for ws in self.admins:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.admins.discard(ws)

    async def to_device(self, did: str, msg: dict) -> None:
        d = self.devices.get(did)
        if d and d.get("ws"):
            try:
                await d["ws"].send_text(json.dumps(msg))
            except Exception:
                pass

    # ---------- session control ----------
    async def start(self, name: str, mode: str, notes: str) -> str | None:
        async with self.lock:
            if self.session:
                return "A session is already running."
            armed = {did: d for did, d in self.devices.items() if d.get("ws") and d.get("armed")}
            if not armed:
                return "No armed recorders connected."
            labels = [d["label"] for d in armed.values()]
            dup = {l for l in labels if labels.count(l) > 1}
            if dup:
                return f"Duplicate device labels: {', '.join(sorted(dup))}"

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:40]
            sid = f"{stamp}_{slug}" if slug else stamp
            sdir = REC_DIR / sid
            sdir.mkdir(parents=True, exist_ok=False)

            meta = {
                "session_id": sid, "name": name, "mode": mode, "notes": notes,
                "started_at_ms": now_ms(), "stopped_at_ms": None,
                "chirp": {"f0": 1000, "f1": 6000, "dur": 0.5},
                "chirps": [], "markers": [], "devices": {},
            }
            members, files = {}, {}
            for did, d in armed.items():
                key = d["label"]
                members[did] = key
                meta["devices"][key] = {
                    "device_id": did, "label": key, "speaker_id": d.get("speaker", ""),
                    "sample_rate": d.get("sample_rate"), "channels": d.get("channels"),
                    "raw_file": f"{key}.f32", "wav_file": f"{key}.wav",
                    "mic": d.get("mic", ""), "clock_rtt_ms": d.get("rtt_ms"),
                    "first_sample_server_ms": None, "frames": None, "user_agent": d.get("ua", ""),
                    "emotions": [], "ui_lang": d.get("lang", ""),
                }
                files[did] = open(sdir / f"{key}.f32", "w+b")
            self.session = {"meta": meta, "dir": sdir, "members": members, "files": files,
                            "finished": set(), "state": "recording"}
            self._save_meta()
        for did in members:
            await self.to_device(did, {"type": "start", "sid": sid})
        return None

    async def stop(self) -> None:
        s = self.session
        if not s or s["state"] != "recording":
            return
        s["state"] = "stopping"
        s["meta"]["stopped_at_ms"] = now_ms()
        self._save_meta()
        for did in s["members"]:
            await self.to_device(did, {"type": "stop", "sid": s["meta"]["session_id"]})

    async def device_finished(self, did: str, sid: str, frames: int, events: list,
                              emotions: list | None = None) -> None:
        s = self.session
        if not s or sid != s["meta"]["session_id"] or did not in s["members"] or did in s["finished"]:
            return
        key = s["members"][did]
        dev = s["meta"]["devices"][key]
        dev["events"] = events
        if emotions is not None:  # the device's own full list replaces the live-streamed copy
            dev["emotions"] = emotions
        f = s["files"].pop(did)
        f.flush()
        f.close()
        dev["frames"] = frames
        await asyncio.to_thread(raw_to_wav, s["dir"] / dev["raw_file"], s["dir"] / dev["wav_file"],
                                dev["sample_rate"], dev["channels"])
        s["finished"].add(did)
        self._save_meta()
        await self.to_device(did, {"type": "saved", "sid": sid, "file": dev["wav_file"], "frames": frames,
                                   "sr": dev["sample_rate"]})
        if s["finished"] == set(s["members"]):
            await self._close()

    async def force_close(self) -> None:
        """Finalize whatever arrived, even if some devices never reported 'finished'."""
        s = self.session
        if not s:
            return
        for did, f in list(s["files"].items()):
            f.flush()
            f.close()
            dev = s["meta"]["devices"][s["members"][did]]
            dev["incomplete"] = True
            dev["frames"] = await asyncio.to_thread(
                raw_to_wav, s["dir"] / dev["raw_file"], s["dir"] / dev["wav_file"],
                dev["sample_rate"], dev["channels"])
        s["files"].clear()
        await self._close()

    async def _close(self) -> None:
        s = self.session
        s["meta"]["closed_at_ms"] = now_ms()
        self._save_meta()
        self.last_session = s["meta"]["session_id"]
        self.session = None
        await self.to_admins({"type": "info", "text": f"Session saved: recordings/{self.last_session}"})

    def _save_meta(self) -> None:
        s = self.session
        (s["dir"] / "session.json").write_text(json.dumps(s["meta"], indent=2, ensure_ascii=False))

    # ---------- audio chunks ----------
    def write_chunk(self, did: str, data: bytes) -> int | None:
        (hlen,) = struct.unpack_from("<I", data, 0)
        hdr = json.loads(data[4:4 + hlen])
        payload = data[4 + hlen:]
        s = self.session
        if not s or hdr["sid"] != s["meta"]["session_id"] or did not in s["files"]:
            return hdr["seq"]  # stale chunk: ack so the client drops it
        dev = s["meta"]["devices"][s["members"][did]]
        if hdr["start"] == 0 and hdr.get("t0_server_ms") is not None:
            dev["first_sample_server_ms"] = hdr["t0_server_ms"]
        f = s["files"][did]
        f.seek(hdr["start"] * hdr["ch"] * 4)
        f.write(payload)
        return hdr["seq"]


hub = Hub()

# ---------------- update check ----------------
# Asks GitHub (public API, nothing about you is sent) whether the repo's main branch is ahead of the
# commit this server is running. Only works for a git checkout (git clone), not a downloaded zip.
UPDATE = {"available": False, "behind": 0, "latest": "", "url": "", "checked": 0}


def git_head() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _check_update_blocking(repo: str, branch: str) -> None:
    head = git_head()
    if not head:
        return
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/compare/{head}...{branch}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "recorder-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.load(r)
    except Exception:
        return  # offline, rate-limited, or this commit isn't on GitHub (local-only work): stay quiet
    behind = j.get("ahead_by", 0) if j.get("status") in ("ahead", "diverged") else 0
    was = UPDATE["available"]
    UPDATE.update(available=behind > 0, behind=behind, checked=now_ms(),
                  latest=((j.get("commits") or [{}])[-1].get("commit", {}).get("message", "") or "").split("\n")[0],
                  url=f"https://github.com/{repo}")
    if behind > 0 and not was:
        print(f"\n  ⬆ Update available: {behind} new commit{'s' if behind != 1 else ''} on {UPDATE['url']}"
              f"\n    \"{UPDATE['latest']}\"\n    Update with:  git pull   then restart this server.\n")


async def update_checker(repo: str, branch: str) -> None:
    while True:
        await asyncio.to_thread(_check_update_blocking, repo, branch)
        await asyncio.sleep(6 * 3600)


def clean_label(s) -> str:
    """Device names are free text but end up in file names, so keep them filesystem-safe."""
    return re.sub(r"[^A-Za-z0-9_-]+", "", str(s or ""))[:24]


def is_speaker(label) -> bool:
    return bool(label) and label not in ("center", "lav")


# ---------------- HTTP routes ----------------
@app.middleware("http")
async def no_cache(request, call_next):
    # Safari caches scripts aggressively; always revalidate so iPads pick up updates.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/take/{sid}/{name}")
async def take(sid: str, name: str):
    """Serve a finished take so the iPad can play back what the server actually saved (supports Range)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid) or not re.fullmatch(r"[A-Za-z0-9_-]+\.wav", name):
        raise HTTPException(404)
    f = REC_DIR / sid / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="audio/wav")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/record")
def record_page():
    return FileResponse(STATIC / "record.html")


@app.get("/control")
def control_page():
    return FileResponse(STATIC / "control.html")


# ---------------- WebSocket ----------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    role, did = None, None
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if role == "device":
                    seq = hub.write_chunk(did, msg["bytes"])
                    await ws.send_text(json.dumps({"type": "ack", "seq": seq}))
                continue
            m = json.loads(msg["text"])
            t = m.get("type")

            if t == "ping":
                await ws.send_text(json.dumps({"type": "pong", "t0": m["t0"], "ts": now_ms()}))
            elif t == "hello":
                role = m["role"]
                if role == "admin":
                    hub.admins.add(ws)
                    await ws.send_text(json.dumps(hub.state()))
                else:
                    did = m["id"]
                    old = hub.devices.get(did, {})
                    m["label"] = clean_label(m.get("label")) or "device"
                    await ws.send_text(json.dumps({"type": "rotate", "on": hub.rotate, "epoch": hub.rotate_epoch}))
                    hub.devices[did] = old | {k: v for k, v in m.items() if k not in ("type", "role", "id")} \
                        | {"ws": ws, "online": True}
                    s = hub.session
                    # Reconnect during a session: tell the device which session is live.
                    if s and did in s["members"] and did not in s["finished"]:
                        await ws.send_text(json.dumps({"type": "resume", "sid": s["meta"]["session_id"],
                                                       "state": s["state"]}))
            elif t == "status" and role == "device":
                hub.devices.setdefault(did, {}).update({k: v for k, v in m.items() if k != "type"})
            elif t == "finished" and role == "device":
                await hub.device_finished(did, m.get("sid"), m["frames"], m.get("events", []),
                                          m.get("emotions"))
            elif t == "emotion" and role == "device":
                # live copy, so labels survive if the iPad page dies before it reports "finished"
                s = hub.session
                if s and did in s["members"] and m.get("sid") == s["meta"]["session_id"]:
                    dev = s["meta"]["devices"][s["members"][did]]
                    dev.setdefault("emotions", []).append(
                        {k: m.get(k) for k in ("frame", "server_ms", "emotion")})
                    hub._save_meta()
            elif role == "admin":
                if t == "start":
                    err = await hub.start(m.get("name", ""), m.get("mode", "B"), m.get("notes", ""))
                    if err:
                        await ws.send_text(json.dumps({"type": "error", "text": err}))
                elif t == "stop":
                    await hub.stop()
                elif t == "force_close":
                    await hub.force_close()
                elif t in ("chirp", "marker") and hub.session:
                    # admin sends its own clock estimate of when the event happened
                    ev = {"server_ms": m.get("server_ms") or now_ms(), "label": m.get("label", "")}
                    key = "chirps" if t == "chirp" else "markers"
                    hub.session["meta"][key].append(ev)
                    hub._save_meta()
                elif t == "identify":
                    await hub.to_device(m["id"], {"type": "identify"})
                elif t == "remove":
                    d = hub.devices.get(m.get("id"))
                    s = hub.session
                    if not d:
                        pass
                    elif d.get("ws"):
                        await ws.send_text(json.dumps({"type": "error", "text": "Device is online; only offline devices can be removed."}))
                    elif s and m["id"] in s["members"] and m["id"] not in s["finished"]:
                        await ws.send_text(json.dumps({"type": "error", "text": "Device is part of the running session; finish or force-save first."}))
                    else:
                        del hub.devices[m["id"]]
                elif t == "view":
                    await hub.to_device(m["id"], {"type": "view", "participant": bool(m.get("participant"))})
                elif t == "lang":
                    await hub.to_device(m["id"], {"type": "lang", "lang": "en" if m.get("lang") == "en" else "ja"})
                elif t == "lang_all":
                    for vid, vd in list(hub.devices.items()):
                        if vd.get("ws") and is_speaker(vd.get("label")):
                            await hub.to_device(vid, {"type": "lang", "lang": "en" if m.get("lang") == "en" else "ja"})
                elif t == "disarm":
                    await hub.to_device(m["id"], {"type": "disarm"})
                elif t == "disarm_all":
                    for vid, vd in list(hub.devices.items()):
                        if vd.get("ws") and is_speaker(vd.get("label")):
                            await hub.to_device(vid, {"type": "disarm"})
                elif t == "rotate_all":
                    on = bool(m.get("on"))
                    if on and not hub.rotate:
                        hub.rotate_epoch += 1
                    hub.rotate = on
                    for vid, vd in list(hub.devices.items()):
                        if vd.get("ws"):
                            await hub.to_device(vid, {"type": "rotate", "on": on, "epoch": hub.rotate_epoch})
                elif t == "view_all":
                    for vid, vd in list(hub.devices.items()):
                        if vd.get("ws") and is_speaker(vd.get("label")):
                            await hub.to_device(vid, {"type": "view", "participant": bool(m.get("participant"))})
                elif t == "assign" and m.get("id") in hub.devices:
                    upd = {k: m[k] for k in ("label", "speaker") if k in m}
                    if "label" in upd:
                        upd["label"] = clean_label(upd["label"]) or "device"
                    hub.devices[m["id"]].update(upd)
                    await hub.to_device(m["id"], {"type": "assign", **upd})
    except WebSocketDisconnect:
        pass
    finally:
        hub.admins.discard(ws)
        if did and hub.devices.get(did, {}).get("ws") is ws:
            hub.devices[did]["ws"] = None
            hub.devices[did]["online"] = False


async def state_pusher():
    while True:
        await asyncio.sleep(0.25)
        if hub.admins:
            await hub.to_admins(hub.state())


@app.on_event("startup")
async def _startup():
    asyncio.create_task(state_pusher())
    if not ARGS.no_update_check:
        asyncio.create_task(update_checker(ARGS.repo, ARGS.branch))


# ---------------- CA helper (plain HTTP) ----------------
ca_app = FastAPI()


def ca_root_pem() -> Path | None:
    if shutil.which("mkcert"):
        root = subprocess.run(["mkcert", "-CAROOT"], capture_output=True, text=True).stdout.strip()
        p = Path(root) / "rootCA.pem"
        if p.exists():
            return p
    return None


@ca_app.get("/")
def ca_index():
    return HTMLResponse(
        "<meta name=viewport content='width=device-width'>"
        "<h2>Step 1: install the certificate</h2>"
        "<p><a href='/rootCA.pem' style='font-size:1.4em'>Download rootCA.pem</a></p>"
        "<p>Then: Settings → General → VPN &amp; Device Management → install the profile.<br>"
        "Then: Settings → General → About → Certificate Trust Settings → enable full trust.</p>"
        "<h2>Step 2</h2><p><a href='/go' style='font-size:1.4em'>Open the recorder (HTTPS)</a></p>")


@ca_app.get("/rootCA.pem")
def ca_file():
    p = ca_root_pem()
    if not p:
        return HTMLResponse("mkcert not installed on the server", status_code=404)
    return FileResponse(p, media_type="application/x-x509-ca-cert", filename="rootCA.pem")


@ca_app.get("/go")
def ca_go():
    return RedirectResponse(f"https://{lan_ip()}:{ARGS.port}/")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


async def main():
    global REC_DIR
    REC_DIR = ARGS.out
    REC_DIR.mkdir(parents=True, exist_ok=True)
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if not cert.exists():
        raise SystemExit("No certs/cert.pem — run ./make_cert.sh first.")
    ip = lan_ip()
    print(f"\n  Recorders : https://{ip}:{ARGS.port}/record")
    print(f"  Control   : https://{ip}:{ARGS.port}/control")
    print(f"  Cert setup: http://{ip}:{ARGS.http_port}/   (first time on each device)\n")
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=ARGS.port, ssl_certfile=str(cert),
                                      ssl_keyfile=str(key), log_level="warning", ws_max_size=64 * 2**20)),
        uvicorn.Server(uvicorn.Config(ca_app, host="0.0.0.0", port=ARGS.http_port, log_level="warning")),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--out", type=Path, default=REC_DIR, help="folder for recordings")
    ap.add_argument("--repo", default="naadimpra/ipad-multitalker-recorder", help="GitHub owner/name to check for updates")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--no-update-check", action="store_true", help="never contact GitHub")
    ARGS = ap.parse_args()
    asyncio.run(main())
