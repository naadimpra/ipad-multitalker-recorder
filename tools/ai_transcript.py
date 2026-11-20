"""What the AI agent said (and heard), on the aligned timeline.

    python -m tools.ai_transcript recordings/<session_id> [--whisper small] [--language ja]

Run after tools.align. Writes aligned/ai_turns.tsv:

  role       "ai" (what the AI said) or "heard" (what the AI service understood the participants said)
  start_s    end_s    on the aligned timeline
  text
  source     "api" (the AI service's own transcript), "whisper:<model>" (transcribed from the AI track),
             or "api, timed by arrival" (only when the transcript arrived is known, a few seconds late)

API scenario: uses the transcripts the AI service sent and the exact moments the AI iPad played each
turn, so no speech recognition is needed. Add --whisper to also transcribe the recorded AI track.
Room / laptop scenario: transcribes aligned/ai.wav with faster-whisper (pip install -r requirements-ai.txt).
In the room scenario the AI iPad also hears the people nearby, so check the result.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from tools.common import load_session


def from_api(meta: dict, adir: Path) -> list[tuple]:
    ai = meta.get("ai") or {}
    tl = adir / "ai_timeline.tsv"
    if not tl.exists():
        print(f"  ! {tl} not found — run python -m tools.align first.")
        return []
    plays: dict[str, list[float]] = defaultdict(list)
    heard_t: dict[int, tuple[float, float, str]] = {}
    with open(tl, newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            a, b = float(r["start_s"]), float(r["end_s"])
            if r["kind"] == "play":
                p = plays[r["id"]]
                p[:] = [min(p[0], a), max(p[1], b)] if p else [a, b]
            else:
                heard_t[int(r["id"])] = (a, b, r["timing"])
    rows = []
    texts = {x["turn"]: x.get("text", "") for x in ai.get("responses", [])}
    for turn, (a, b) in plays.items():
        rows.append(("ai", a, b, texts.get(turn, ""), "api"))
    missing = [t for t in texts if t not in plays and texts[t]]
    if missing:
        print(f"  ! {len(missing)} AI responses were never played (interrupted before audio arrived?)")
    for i, h in enumerate(ai.get("heard", [])):
        if i in heard_t:
            a, b, timing = heard_t[i]
            rows.append(("heard", a, b, h.get("text", ""), "api" if timing == "frames" else "api, timed by arrival"))
    return rows


def from_whisper(adir: Path, key: str, model: str, language: str | None) -> list[tuple]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("faster-whisper is not installed: .venv/bin/pip install -r requirements-ai.txt")
    wav = adir / f"{key}.wav"
    if not wav.exists():
        raise SystemExit(f"{wav} not found — run python -m tools.align first.")
    print(f"Transcribing {wav.name} with whisper '{model}' (first run downloads the model)…")
    m = WhisperModel(model, device="auto", compute_type="auto")
    segs, info = m.transcribe(str(wav), language=language, vad_filter=True)
    rows = [("ai", s.start, s.end, s.text.strip(), f"whisper:{model}") for s in segs if s.text.strip()]
    print(f"  language: {info.language}, {len(rows)} segments")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--whisper", default=None, metavar="MODEL",
                    help="transcribe the AI track with faster-whisper (tiny/base/small/medium/large-v3). "
                         "Default for room/laptop sessions: small")
    ap.add_argument("--language", default=None, help="e.g. ja or en (default: detect)")
    ap.add_argument("--device", default=None, help="AI device name (default: from session.json, else 'ai')")
    args = ap.parse_args()

    meta = load_session(args.session)
    adir = args.session / "aligned"
    ai = meta.get("ai") or {}
    key = args.device or ai.get("device") or "ai"
    scenario = ai.get("scenario", "none")
    if key not in meta["devices"]:
        raise SystemExit(f"No AI device '{key}' in this session (devices: {', '.join(meta['devices'])}).")
    print(f"AI agent: {ai.get('agent') or ai.get('provider') or '?'}  (scenario: {scenario}, device: {key})")

    rows = []
    if scenario == "api":
        rows += from_api(meta, adir)
    if args.whisper or scenario != "api":
        rows += from_whisper(adir, key, args.whisper or "small", args.language)

    out = adir / "ai_turns.tsv"
    with open(out, "w") as f:
        f.write("role\tstart_s\tend_s\ttext\tsource\n")
        for role, a, b, text, src in sorted(rows, key=lambda r: (r[1], r[0])):
            text = " ".join(str(text).split())
            f.write(f"{role}\t{a:.3f}\t{b:.3f}\t{text}\t{src}\n")
    n_ai = sum(r[0] == "ai" for r in rows)
    print(f"Wrote {out}  ({n_ai} AI turns, {len(rows) - n_ai} heard)")


if __name__ == "__main__":
    main()
