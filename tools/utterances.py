"""Attach emotion labels to each speaker's speech segments.

    python -m tools.utterances recordings/<session_id> [--rttm FILE] [--shift SEC] [--min-piece SEC]

Inputs (all on the aligned timeline):
  aligned/draft.rttm     who speaks when (from tools.draft_rttm, or your hand-corrected RTTM via --rttm)
  aligned/emotions.tsv   the emotion each participant had selected, as time ranges (from tools.align)

A participant's emotion button stays selected until they tap another one, so the label only means
something while they speak. This tool cuts each speech segment at the moments the emotion changed:

  one speech segment 12.0-20.0 s, emotion changed to "angry" at 16.0 s
  -> 12.0-16.0 happy   and   16.0-20.0 angry

Silence gets no label. Output: aligned/utterances.tsv (speaker, device, start_s, end_s, dur_s, emotion).
"""
from __future__ import annotations

import argparse
import bisect
import csv
from collections import defaultdict
from pathlib import Path

from tools.common import load_session

UNLABELED = "unlabeled"


def read_rttm(path: Path) -> dict[str, list[tuple[float, float]]]:
    segs: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for line in path.read_text().splitlines():
        f = line.split()
        if len(f) >= 8 and f[0] == "SPEAKER":
            start, dur = float(f[3]), float(f[4])
            segs[f[7]].append((start, start + dur))
    return {k: sorted(v) for k, v in segs.items()}


def read_emotions(path: Path, shift: float) -> dict[str, list[tuple[float, float, str]]]:
    """device -> [(start, end, emotion)], boundaries moved `shift` seconds earlier."""
    out: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            out[r["device"]].append((float(r["start_s"]) - shift, float(r["end_s"]) - shift, r["emotion"]))
    return {k: sorted(v) for k, v in out.items()}


def split_segment(s: float, e: float, tl: list[tuple[float, float, str]], min_piece: float) -> list[list]:
    """Cut [s, e) at emotion boundaries -> [[start, end, emotion], ...]."""
    starts = [a for a, _, _ in tl]
    cuts = sorted({s, e, *[p for a, b, _ in tl for p in (a, b) if s < p < e]})
    pieces: list[list] = []
    for p, q in zip(cuts, cuts[1:]):
        m = (p + q) / 2
        i = bisect.bisect_right(starts, m) - 1
        emo = tl[i][2] if i >= 0 and m < tl[i][1] else UNLABELED
        if pieces and pieces[-1][2] == emo:
            pieces[-1][1] = q
        else:
            pieces.append([p, q, emo])
    # A tap that lands just after speech began would leave a sliver; fold slivers into a neighbour.
    while len(pieces) > 1:
        i = min(range(len(pieces)), key=lambda k: pieces[k][1] - pieces[k][0])
        if pieces[i][1] - pieces[i][0] >= min_piece:
            break
        left = pieces[i - 1] if i > 0 else None
        right = pieces[i + 1] if i + 1 < len(pieces) else None
        into = max((n for n in (left, right) if n), key=lambda n: n[1] - n[0])
        into[0], into[1] = min(into[0], pieces[i][0]), max(into[1], pieces[i][1])
        pieces.pop(i)
        merged: list[list] = []
        for p in pieces:
            if merged and merged[-1][2] == p[2]:
                merged[-1][1] = p[1]
            else:
                merged.append(p)
        pieces = merged
    return pieces


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--rttm", type=Path, default=None, help="speech segments (default: aligned/draft.rttm)")
    ap.add_argument("--shift", type=float, default=0.0,
                    help="move emotion changes this many seconds EARLIER (people tap after the feeling starts)")
    ap.add_argument("--min-piece", type=float, default=0.3,
                    help="pieces shorter than this are merged into a neighbour (default 0.3 s)")
    args = ap.parse_args()

    adir = args.session / "aligned"
    rttm = args.rttm or adir / "draft.rttm"
    emo_file = adir / "emotions.tsv"
    if not rttm.exists():
        raise SystemExit(f"{rttm} not found. Run: python -m tools.draft_rttm {args.session}")
    if not emo_file.exists():
        raise SystemExit(f"{emo_file} not found. Run: python -m tools.align {args.session} "
                         "(and make sure emotions were tapped during the recording).")

    meta = load_session(args.session)
    # An RTTM speaker may be named by device (spk1) or by participant ID (P001).
    to_device: dict[str, str] = {}
    for key, d in meta["devices"].items():
        to_device[key] = key
        if d.get("speaker_id"):
            to_device[d["speaker_id"]] = key

    speech = read_rttm(rttm)
    timelines = read_emotions(emo_file, args.shift)
    rows, totals = [], defaultdict(float)
    for spk, segs in sorted(speech.items()):
        dev = to_device.get(spk)
        if dev is None or dev not in timelines:
            print(f"  ! no emotion labels for speaker '{spk}' -> '{UNLABELED}'")
        tl = timelines.get(dev or "", [])
        for s, e in segs:
            for a, b, emo in split_segment(s, e, tl, args.min_piece):
                rows.append((spk, dev or "", a, b, emo))
                totals[(spk, emo)] += b - a

    rows.sort(key=lambda r: (r[2], r[0]))
    out = adir / "utterances.tsv"
    with open(out, "w") as f:
        f.write("speaker\tdevice\tstart_s\tend_s\tdur_s\temotion\n")
        for spk, dev, a, b, emo in rows:
            f.write(f"{spk}\t{dev}\t{a:.3f}\t{b:.3f}\t{b - a:.3f}\t{emo}\n")

    print(f"Wrote {out}  ({len(rows)} labelled speech segments)")
    for (spk, emo), t in sorted(totals.items()):
        print(f"  {spk:10s} {emo:10s} {t:7.1f} s")


if __name__ == "__main__":
    main()
