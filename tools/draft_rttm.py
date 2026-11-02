"""Draft speaker labels (RTTM) from close-talk channels — a starting point for human correction.

    python -m tools.draft_rttm recordings/<session_id>

Mode B: uses aligned/spk1, spk2, ... (one iPad near each speaker, any number).
Mode A: uses aligned/lav.wav only if it has one channel per speaker; a single
        mixed channel needs a diarization model instead (e.g. pyannote).

Per 20 ms frame, a speaker is active when their channel is clearly above its
own noise floor AND within --overlap-db of the loudest channel (so crosstalk
picked up by other speakers' mics is rejected, but real overlap is kept).

Overlap = 2+ speakers active at once for at least --min-overlap seconds, reported as a share of
speech time (silence excluded). Regions are written to aligned/overlap.tsv.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import median_filter
from scipy.signal import butter, sosfilt, sosfilt_zi

from tools.common import load_session

HOP = 0.02


def frame_energy_db(path: Path, channel: int | None) -> tuple[np.ndarray, int]:
    sr = sf.info(str(path)).samplerate
    hop = int(HOP * sr)
    sos = butter(4, 100, "highpass", fs=sr, output="sos")
    zi = None
    out, carry = [], np.zeros(0, np.float32)
    for blk in sf.blocks(str(path), blocksize=hop * 500, dtype="float32", always_2d=True):
        x = blk[:, channel] if channel is not None else blk.mean(axis=1)
        if zi is None:
            zi = sosfilt_zi(sos) * x[0]
        x, zi = sosfilt(sos, x, zi=zi)
        x = np.concatenate([carry, x])
        n = len(x) // hop
        fr = x[: n * hop].reshape(n, hop)
        out.append(10 * np.log10(np.mean(fr * fr, axis=1) + 1e-12))
        carry = x[n * hop:]
    return np.concatenate(out), sr


def segments(active: np.ndarray, min_dur: float, min_gap: float) -> list[tuple[float, float]]:
    act = active.astype(np.int8)
    edges = np.diff(np.concatenate([[0], act, [0]]))
    segs = list(zip(np.where(edges == 1)[0] * HOP, np.where(edges == -1)[0] * HOP))
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s - merged[-1][1] < min_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= min_dur]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--snr-db", type=float, default=12.0, help="min dB above channel noise floor")
    ap.add_argument("--overlap-db", type=float, default=6.0, help="max dB below loudest channel to count as overlap")
    ap.add_argument("--min-dur", type=float, default=0.25)
    ap.add_argument("--min-gap", type=float, default=0.30)
    ap.add_argument("--min-overlap", type=float, default=0.10,
                    help="only count overlap where 2+ speakers talk together at least this long (s)")
    args = ap.parse_args()

    sdir: Path = args.session
    meta = load_session(sdir)
    adir = sdir / "aligned"
    if not adir.exists():
        raise SystemExit("Run python -m tools.align first.")

    chans: list[tuple[str, Path, int | None]] = []  # (speaker name, file, channel)
    for key in sorted(meta["devices"]):
        d = meta["devices"][key]
        f = adir / f"{key}.wav"
        if key not in ("center", "lav") and f.exists():
            chans.append((d.get("speaker_id") or key, f, None))
    if not chans and (adir / "lav.wav").exists():
        nch = sf.info(str(adir / "lav.wav")).channels
        if nch < 2:
            raise SystemExit("lav.wav is a single mixed channel — energy-based labels are impossible.\n"
                             "Use a diarization model (e.g. pyannote/speaker-diarization) for the draft.")
        chans = [(f"lav_ch{c + 1}", adir / "lav.wav", c) for c in range(nch)]
    if len(chans) < 2:
        raise SystemExit("Need at least 2 close-talk channels.")

    energies = [frame_energy_db(f, c)[0] for _, f, c in chans]
    n = min(len(e) for e in energies)
    E = np.stack([e[:n] for e in energies])               # (spk, frames)
    snr = E - np.percentile(E, 10, axis=1, keepdims=True)  # normalise per-channel gain
    loudest = snr.max(axis=0, keepdims=True)
    active = (snr > args.snr_db) & (snr >= loudest - args.overlap_db)
    active = median_filter(active.astype(np.uint8), size=(1, 7)) > 0

    sid = meta["session_id"]
    lines, total = [], {}
    final = np.zeros_like(active)  # what ends up in the RTTM, frame by frame
    for i, ((name, _, _), act) in enumerate(zip(chans, active)):
        segs = segments(act, args.min_dur, args.min_gap)
        for s, e in segs:
            final[i, int(round(s / HOP)):int(round(e / HOP))] = True
        total[name] = sum(e - s for s, e in segs)
        lines += [(s, f"SPEAKER {sid} 1 {s:.3f} {e - s:.3f} <NA> <NA> {name} <NA> <NA>") for s, e in segs]
    lines.sort()
    (adir / "draft.rttm").write_text("\n".join(l for _, l in lines) + "\n")

    # Overlap = 2+ speakers talking at once, measured against SPEECH time (silence excluded).
    dur = n * HOP
    n_act = final.sum(axis=0)
    speech_s = float((n_act >= 1).sum() * HOP)
    regions = segments(n_act >= 2, args.min_overlap, 0.0)
    overlap_s = sum(e - s for s, e in regions)
    with open(adir / "overlap.tsv", "w") as f:
        f.write("start_s\tend_s\tdur_s\tspeakers\n")
        for s, e in regions:
            who = [chans[k][0] for k in range(len(chans)) if final[k, int(round(s / HOP)):int(round(e / HOP))].any()]
            f.write(f"{s:.3f}\t{e:.3f}\t{e - s:.3f}\t{','.join(who)}\n")

    print(f"Wrote {adir / 'draft.rttm'}  ({len(lines)} segments, {dur / 60:.1f} min)")
    for name, t in total.items():
        print(f"  {name:10s} {t / 60:6.1f} min  ({t / dur:5.1%} of recording)")
    print(f"  speech (anyone talking): {speech_s / 60:.1f} min  ({speech_s / dur:.1%} of recording)")
    if speech_s:
        print(f"  overlap: {len(regions)} regions, {overlap_s:.1f} s = {overlap_s / speech_s:.1%} of speech time "
              f"(2+ speakers together for >= {args.min_overlap:g} s) -> {adir / 'overlap.tsv'}")
    else:
        print("  no speech detected")


if __name__ == "__main__":
    main()
