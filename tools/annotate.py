"""Transcribe a session in the browser: waveform, playback and four lines per speech segment
(text as written, yomigana, pitch accent, romaji; the readings are drafted by tools.reading and then
corrected, the pitch is filled in by hand: one of L / H / D per mora of the yomigana).

    python -m tools.annotate recordings/<session>     (or the recordings folder, to pick a session)

Opens http://127.0.0.1:8765/annotate on this machine only. The recording server serves the same page
at /annotate, so either way works; this launcher needs no certificate and no iPads.

Timeline: if the session has been aligned (tools.align) the aligned tracks are used and the transcript
is saved in aligned/, next to draft.rttm and utterances.tsv. Otherwise the raw tracks are used and the
transcript is saved in the session folder (fine for one device; align first when there are several).

Output, rewritten on every change:
  transcript.json   segments (device, speaker, start, end, text, kana, pitch, romaji) - what the page loads again
  transcript.tsv    speaker, device, start_s, end_s, dur_s, emotion, text, kana, pitch, romaji - the columns of
                    utterances.tsv plus the transcript lines; emotion is the one tapped during most of the segment

In the yomigana a ＊ after a mora marks a devoiced vowel (し＊ -> shI in the romaji). The pitch line has
one letter per mora, grouped like the words of the yomigana: L low, H high, D devoiced (and low),
_ not filled in yet. Example: です＊ -> pitch "LD", romaji "desU".
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import APIRouter, Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from scipy.ndimage import median_filter

from tools import reading
from tools.common import CHIRP_DUR, load_session
from tools.draft_rttm import frame_energy_db, segments
from tools.utterances import UNLABELED, read_rttm

STATIC = Path(__file__).parent.parent / "static"
PEAK_HZ = 200          # waveform resolution sent to the page (min/max pairs per second, roughly)
ROOM_MICS = ("center", "lav")
MAX_SEGMENTS, MAX_TEXT = 20000, 5000
SCRIPTS = ("text", "kana", "pitch", "romaji")  # as written (kanji), yomigana, pitch per mora, romaji
_save_lock = threading.Lock()


# ---------------- session layout ----------------
def find_session(rec: Path, sid: str) -> tuple[Path, dict]:
    """Session folder + its session.json. `sid` is the folder name (folders may have been renamed)."""
    if not sid or sid.startswith(".") or any(c in sid for c in "/\\\x00"):
        raise HTTPException(404)
    sdir = rec / sid
    if not (sdir / "session.json").is_file():
        raise HTTPException(404)
    return sdir, load_session(sdir)


def timeline(sdir: Path, meta: dict) -> tuple[str, Path]:
    """("aligned", aligned/) once tools.align has run, else ("raw", session folder)."""
    adir = sdir / "aligned"
    if any((adir / f"{k}.wav").is_file() for k in meta["devices"]):
        return "aligned", adir
    return "raw", sdir


def track_files(meta: dict, base: Path) -> dict[str, Path]:
    """device -> WAV on the current timeline; speaker iPads first, room mics last."""
    keys = sorted(meta["devices"], key=lambda k: (k in ROOM_MICS, k))
    return {k: base / f"{k}.wav" for k in keys if (base / f"{k}.wav").is_file()}


def speaker_name(meta: dict, key: str) -> str:
    return meta["devices"][key].get("speaker_id") or key


def emotion_ranges(sdir: Path, meta: dict, tl: str, base: Path, durations: dict[str, float]) -> dict[str, list]:
    """device -> [[start_s, end_s, emotion], ...] on the current timeline."""
    out: dict[str, list] = defaultdict(list)
    if tl == "aligned":
        f = base / "emotions.tsv"
        if f.exists():
            with open(f, newline="") as fh:
                for r in csv.DictReader(fh, delimiter="\t"):
                    out[r["device"]].append([float(r["start_s"]), float(r["end_s"]), r["emotion"]])
    else:
        for key, d in meta["devices"].items():
            sr, dur = d.get("sample_rate"), durations.get(key)
            if not sr or dur is None:
                continue
            evs = sorted(d.get("emotions") or [], key=lambda e: e["frame"])
            for i, e in enumerate(evs):
                a = e["frame"] / sr
                b = evs[i + 1]["frame"] / sr if i + 1 < len(evs) else dur
                if b > a:
                    out[key].append([round(a, 3), round(min(b, dur), 3), e["emotion"]])
    return {k: sorted(v) for k, v in out.items()}


def chirp_times(sdir: Path, meta: dict, tl: str, key: str) -> list[float]:
    """Where the sync chirps sit in one track (seconds, roughly +/- 0.1 s)."""
    chirps = [c for c in meta.get("chirps", []) if c.get("server_ms")]
    if tl == "raw":
        t0 = meta["devices"][key].get("first_sample_server_ms")
        return [(c["server_ms"] - t0) / 1000 for c in chirps] if t0 else []
    try:
        start_at = json.loads((sdir / "aligned" / "alignment.json").read_text())["start_chirp_at_s"]
    except (OSError, ValueError, KeyError):
        return []
    ref = next((c["server_ms"] for c in chirps if c.get("label") == "start"), None)
    return [start_at + (c["server_ms"] - ref) / 1000 for c in chirps] if ref else []


def marks(sdir: Path, meta: dict, tl: str, base: Path, first_key: str | None) -> list[dict]:
    """Admin markers and chirps for the ruler. Raw timeline: relative to the first track."""
    out = []
    if first_key:
        out += [{"t": round(t, 3), "label": "chirp", "kind": "chirp"} for t in chirp_times(sdir, meta, tl, first_key)]
    if tl == "aligned":
        f = base / "markers.tsv"
        if f.exists():
            with open(f, newline="") as fh:
                out += [{"t": float(r["time_s"]), "label": r["label"] or "marker", "kind": "marker"}
                        for r in csv.DictReader(fh, delimiter="\t")]
    elif first_key:
        t0 = meta["devices"][first_key].get("first_sample_server_ms")
        if t0:
            out += [{"t": round((m["server_ms"] - t0) / 1000, 3), "label": m.get("label") or "marker", "kind": "marker"}
                    for m in meta.get("markers", []) if m.get("server_ms")]
    return sorted((m for m in out if m["t"] >= 0), key=lambda m: m["t"])


# ---------------- waveform peaks ----------------
@lru_cache(maxsize=24)
def _peaks(path: str, mtime_ns: int, size: int) -> bytes:
    """int8 (min, max) pairs, one pair per sr // PEAK_HZ samples. Cached per file version."""
    info = sf.info(path)
    hop = max(1, info.samplerate // PEAK_HZ)
    lo, hi, carry = [], [], np.zeros(0, np.float32)
    for blk in sf.blocks(path, blocksize=hop * 4000, dtype="float32", always_2d=True):
        x = np.concatenate([carry, blk.mean(axis=1)])
        n = len(x) // hop
        fr = x[: n * hop].reshape(n, hop)
        lo.append(fr.min(axis=1))
        hi.append(fr.max(axis=1))
        carry = x[n * hop:]
    if len(carry):
        lo.append(np.array([carry.min()]))
        hi.append(np.array([carry.max()]))
    if not lo:
        return b""
    lo, hi = np.concatenate(lo), np.concatenate(hi)
    # Scale to the loud end of the speech, not to the single loudest click (the chirp), and clip.
    ref = max(float(np.percentile(np.maximum(-lo, hi), 99.8)), 1e-6)
    q = np.empty(2 * len(lo), np.int8)
    q[0::2] = np.clip(np.round(lo / ref * 100), -127, 127)
    q[1::2] = np.clip(np.round(hi / ref * 100), -127, 127)
    return q.tobytes()


def peaks(path: Path) -> bytes:
    st = path.stat()
    return _peaks(str(path), st.st_mtime_ns, st.st_size)


# ---------------- draft segments ----------------
def silence_segments(path: Path, chirps: list[float], snr_db: float = 12.0, below_loud_db: float = 25.0,
                     min_dur: float = 0.3, min_gap: float = 0.45, pad: float = 0.12) -> list[tuple[float, float]]:
    """Speech stretches of one track, split at pauses. A starting point, like draft.rttm.

    A frame counts as speech when it is clearly above the noise floor AND within `below_loud_db` of
    the loud end of the track; the second test is what separates sentences, because breaths and room
    sound between them sit well above the floor of a quiet room.
    """
    e, _ = frame_energy_db(path, None)
    if not len(e):
        return []
    dur = sf.info(str(path)).duration
    thr = max(np.percentile(e, 10) + snr_db, np.percentile(e, 95) - below_loud_db)
    active = median_filter((e > thr).astype(np.uint8), size=7) > 0
    out = []
    for s, t in segments(active, min_dur, min_gap):
        # the sync chirps are loud but are not speech
        in_chirp = sum(max(0.0, min(t, c + CHIRP_DUR + 0.4) - max(s, c - 0.4)) for c in chirps)
        if in_chirp < 0.6 * (t - s):
            out.append([float(s), float(t)])
    for i, seg in enumerate(out):  # a little air on both sides, without touching the neighbours
        lo = (out[i - 1][1] + seg[0]) / 2 if i else 0.0
        seg[0] = max(lo, seg[0] - pad)
    for i, seg in enumerate(out):
        hi = (seg[1] + out[i + 1][0]) / 2 if i + 1 < len(out) else dur
        seg[1] = min(hi, seg[1] + pad, dur)
    return [(round(s, 3), round(t, 3)) for s, t in out]


def draft(sdir: Path, meta: dict, tl: str, base: Path, devices: list[str]) -> dict:
    """Draft segments for `devices`: utterances.tsv, else draft.rttm, else silence detection."""
    files = track_files(meta, base)
    want = [d for d in devices if d in files]
    to_device = {k: k for k in meta["devices"]} | \
                {d["speaker_id"]: k for k, d in meta["devices"].items() if d.get("speaker_id")}
    rows: list[dict] = []
    source = ""
    if tl == "aligned" and (base / "utterances.tsv").exists():
        source = "utterances.tsv"
        with open(base / "utterances.tsv", newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                dev = r.get("device") or to_device.get(r["speaker"], "")
                rows.append({"device": dev, "start": float(r["start_s"]), "end": float(r["end_s"])})
    elif tl == "aligned" and (base / "draft.rttm").exists():
        source = "draft.rttm"
        for spk, segs in read_rttm(base / "draft.rttm").items():
            rows += [{"device": to_device.get(spk, ""), "start": s, "end": e} for s, e in segs]
    if source:
        rows = [r for r in rows if r["device"] in want]
    else:
        source = "silence"
        speakers = [d for d in want if d not in ROOM_MICS]
        for dev in speakers or want:  # a room mic hears everyone: only draft it when it is all there is
            for s, e in silence_segments(files[dev], chirp_times(sdir, meta, tl, dev)):
                rows.append({"device": dev, "start": s, "end": e})
    rows.sort(key=lambda r: (r["start"], r["device"]))
    return {"source": source, "segments": rows}


# ---------------- transcript ----------------
def load_transcript(base: Path) -> dict:
    try:
        t = json.loads((base / "transcript.json").read_text())
        segs = [{k: g.get(k, "") for k in ("id", "device", "start", "end", *SCRIPTS)} for g in t.get("segments", [])]
        return {"rev": int(t.get("rev", 0)), "segments": segs}
    except (OSError, ValueError):
        return {"rev": 0, "segments": []}


def _emotion_for(ranges: list, s: float, e: float) -> str:
    best, best_ov = UNLABELED, 0.0
    for a, b, emo in ranges:
        ov = min(e, b) - max(s, a)
        if ov > best_ov:
            best, best_ov = emo, ov
    return best


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # never leaves a half-written transcript behind


def save_transcript(sdir: Path, meta: dict, body: dict) -> int:
    tl, base = timeline(sdir, meta)
    files = track_files(meta, base)
    raw = body.get("segments")
    if not isinstance(raw, list) or len(raw) > MAX_SEGMENTS:
        raise HTTPException(400, "bad segments")
    segs = []
    for r in raw:
        try:
            s, e, dev = float(r["start"]), float(r["end"]), r["device"]
            scripts = {k: str(r.get(k) or "")[:MAX_TEXT] for k in SCRIPTS}
            scripts["pitch"] = re.sub(r"[^LHD_ ]", "", scripts["pitch"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "bad segment")
        if dev not in files or not (0 <= s < e):
            raise HTTPException(400, "bad segment")
        segs.append({"id": str(r.get("id", ""))[:32], "device": dev, "speaker": speaker_name(meta, dev),
                     "start": round(s, 3), "end": round(e, 3), **scripts})
    segs.sort(key=lambda g: (g["start"], g["device"]))
    durations = {k: sf.info(str(f)).duration for k, f in files.items()}
    emo = emotion_ranges(sdir, meta, tl, base, durations)
    with _save_lock:
        rev = load_transcript(base)["rev"]
        if body.get("rev") != rev:  # another window saved in between: don't overwrite its work
            raise HTTPException(409, "transcript changed elsewhere")
        rev += 1
        doc = {"version": 1, "session_id": meta.get("session_id", sdir.name), "timeline": tl, "rev": rev,
               "saved_at": datetime.now().isoformat(timespec="seconds"), "segments": segs}
        _write(base / "transcript.json", json.dumps(doc, indent=1, ensure_ascii=False))
        lines = ["speaker\tdevice\tstart_s\tend_s\tdur_s\temotion\t" + "\t".join(SCRIPTS)]
        for g in segs:
            scripts = "\t".join(" ".join(g[k].split()) for k in SCRIPTS)  # tabs / newlines would break the table
            lines.append(f"{g['speaker']}\t{g['device']}\t{g['start']:.3f}\t{g['end']:.3f}\t"
                         f"{g['end'] - g['start']:.3f}\t{_emotion_for(emo.get(g['device'], []), g['start'], g['end'])}\t{scripts}")
        _write(base / "transcript.tsv", "\n".join(lines) + "\n")
    return rev


# ---------------- HTTP ----------------
def make_router(rec_dir: Callable[[], Path]) -> APIRouter:
    r = APIRouter()

    @r.get("/annotate")
    def page():
        return FileResponse(STATIC / "annotate.html")

    @r.get("/api/annotate/sessions")
    def sessions():
        out = []
        root = rec_dir()
        for sdir in root.iterdir() if root.is_dir() else []:
            if sdir.name.startswith(".") or not (sdir / "session.json").is_file():
                continue
            try:
                meta = load_session(sdir)
                tl, base = timeline(sdir, meta)
                segs = load_transcript(base)["segments"]
                dur = max((d["frames"] / d["sample_rate"] for d in meta["devices"].values()
                           if d.get("frames") and d.get("sample_rate")), default=None)
                out.append({"id": sdir.name, "name": meta.get("name") or sdir.name, "timeline": tl,
                            "started_at_ms": meta.get("started_at_ms") or 0, "duration": dur,
                            "tracks": list(track_files(meta, base)),
                            "segments": len(segs), "done": sum(1 for g in segs if str(g.get("text", "")).strip())})
            except (OSError, ValueError, KeyError, TypeError):
                continue  # a broken session folder should not hide the others
        return sorted(out, key=lambda s: -s["started_at_ms"])

    @r.get("/api/annotate/s/{sid}")
    def info(sid: str):
        sdir, meta = find_session(rec_dir(), sid)
        tl, base = timeline(sdir, meta)
        files = track_files(meta, base)
        infos = {k: sf.info(str(f)) for k, f in files.items()}
        emo = emotion_ranges(sdir, meta, tl, base, {k: i.duration for k, i in infos.items()})
        url = f"/api/annotate/s/{quote(sid)}"
        tracks = [{"device": k, "speaker": speaker_name(meta, k), "duration": i.duration,
                   "peaks_rate": i.samplerate / max(1, i.samplerate // PEAK_HZ),
                   "audio": f"{url}/audio/{k}", "peaks": f"{url}/peaks/{k}", "emotions": emo.get(k, [])}
                  for k, i in infos.items()]
        return {"id": sid, "name": meta.get("name") or sid, "timeline": tl, "tracks": tracks,
                "mix": f"{url}/audio/mix_mono" if tl == "aligned" and (base / "mix_mono.wav").is_file() else None,
                "marks": marks(sdir, meta, tl, base, next(iter(files), None)),
                "readings": reading.available(),  # can yomigana be drafted from the text?
                "transcript": load_transcript(base)}

    def wav(sid: str, name: str) -> Path:
        sdir, meta = find_session(rec_dir(), sid)
        base = timeline(sdir, meta)[1]
        if name != "mix_mono" and name not in meta["devices"]:
            raise HTTPException(404)
        f = base / f"{name}.wav"
        if not f.is_file():
            raise HTTPException(404)
        return f

    @r.get("/api/annotate/s/{sid}/audio/{name}")
    def audio(sid: str, name: str):
        return FileResponse(wav(sid, name), media_type="audio/wav")  # supports Range, so seeking is instant

    @r.get("/api/annotate/s/{sid}/peaks/{name}")
    def peaks_route(sid: str, name: str):
        return Response(peaks(wav(sid, name)), media_type="application/octet-stream")

    @r.get("/api/annotate/s/{sid}/draft")
    def draft_route(sid: str, devices: str = ""):
        sdir, meta = find_session(rec_dir(), sid)
        tl, base = timeline(sdir, meta)
        return draft(sdir, meta, tl, base, [d for d in devices.split(",") if d])

    @r.post("/api/annotate/reading")
    def reading_route(body: dict = Body(...)):
        """Draft readings. {text, prev_text} -> kana + romaji; {kana, prev_kana} -> romaji.
        The prev_* results tell the page whether a field still holds an untouched draft."""
        cut = lambda k: str(body.get(k) or "")[:MAX_TEXT]
        if "text" in body:
            new, old = reading.readings(cut("text")), reading.readings(cut("prev_text"))
            return {"kana": new["kana"], "romaji": new["romaji"], "prev_kana": old["kana"], "prev_romaji": old["romaji"]}
        return {"romaji": reading.romaji(cut("kana")), "prev_romaji": reading.romaji(cut("prev_kana"))}

    @r.put("/api/annotate/s/{sid}/transcript")
    def save(sid: str, body: dict = Body(...)):
        sdir, meta = find_session(rec_dir(), sid)
        return {"rev": save_transcript(sdir, meta, body)}

    return r


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a session in the browser.")
    ap.add_argument("path", type=Path, nargs="?", default=Path(__file__).parent.parent / "recordings",
                    help="a session folder, or the folder that holds the sessions (default: recordings/)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    path = args.path.resolve()
    is_session = (path / "session.json").is_file()
    rec = path.parent if is_session else path
    if not rec.is_dir():
        raise SystemExit(f"{args.path} not found.")

    app = FastAPI()

    @app.middleware("http")
    async def no_cache(request, call_next):  # always pick up page updates
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        return RedirectResponse("/annotate")

    app.include_router(make_router(lambda: rec))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    url = f"http://127.0.0.1:{args.port}/annotate" + (f"?s={quote(path.name)}" if is_session else "")
    print(f"\n  Transcribe: {url}\n  (Ctrl+C to stop)\n")
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
