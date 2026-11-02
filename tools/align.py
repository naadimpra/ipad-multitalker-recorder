"""Sample-align all device recordings of a session using the start/end sync chirps.

    python -m tools.align recordings/<session_id> [--ref center]

For each device it finds the start and end chirp by matched filtering, then
  * drift  = (end - start) interval vs. the reference device -> resample (soxr)
  * offset = start chirp position -> trim / pad
and writes recordings/<session>/aligned/<label>.wav (all same length, same t=0),
alignment.json and markers.tsv (admin markers on the aligned timeline).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from scipy.signal import fftconvolve, hilbert

from tools.combine import combine
from tools.common import chirp, load_session

BLOCK = 48000 * 10


def find_chirp(path: Path, sr: int, start_s: float, end_s: float) -> tuple[int | None, float]:
    """Return (sample index of chirp onset, confidence) within [start_s, end_s)."""
    info = sf.info(str(path))
    a = max(0, int(start_s * sr))
    b = min(info.frames, int(end_s * sr))
    if b - a < len(chirp(sr)) * 2:
        return None, 0.0
    x, _ = sf.read(str(path), start=a, stop=b, dtype="float64", always_2d=True)
    x = x.mean(axis=1)
    c = chirp(sr).astype(np.float64)
    corr = fftconvolve(x, c[::-1], mode="valid")
    env = np.abs(hilbert(corr))
    # Normalised correlation (0..1): peak / (|chirp| * local signal energy). Noise-only
    # audio scores ~0.01-0.03, a clearly audible chirp scores 0.2 and up.
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    local = cs[len(c):] - cs[:-len(c)]
    score = env / np.sqrt(np.dot(c, c) * local + 1e-12)
    i = int(np.argmax(score))
    return a + i, float(score[i])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--ref", default=None, help="reference device label (default: center, else first)")
    ap.add_argument("--search", type=float, default=30.0, help="seconds searched at start/end for chirps")
    ap.add_argument("--min-conf", type=float, default=0.08, help="min normalised correlation (0-1) to accept a chirp")
    ap.add_argument("--tol", type=float, default=3.0,
                    help="search +/- this many seconds around the chirp time expected from session timestamps")
    ap.add_argument("--no-combine", action="store_true", help="don't write combined.wav / mix_mono.wav")
    args = ap.parse_args()

    sdir: Path = args.session
    meta = load_session(sdir)
    devs = {k: v for k, v in meta["devices"].items() if (sdir / v["wav_file"]).exists()}
    if not devs:
        raise SystemExit("No WAV files found. If the server crashed, run: python -m tools.common " + str(sdir))
    ref = args.ref or ("center" if "center" in devs else sorted(devs)[0])
    print(f"Reference device: {ref}")

    # ---- 1. detect chirps ----
    # Where each chirp should be, from the admin phone's timestamps. When known, search only
    # around it; otherwise fall back to the first/last --search seconds.
    chirp_ms = {}
    for c in meta.get("chirps", []):
        if c.get("server_ms") and c["label"] in ("start", "end"):
            chirp_ms[c["label"]] = c["server_ms"]
    warnings: list[str] = []
    det = {}
    for key, d in devs.items():
        p, sr = sdir / d["wav_file"], d["sample_rate"]
        n = sf.info(str(p)).frames
        dur = n / sr
        t0 = d.get("first_sample_server_ms")
        found = {}
        for which, default_win in (("start", (0, min(args.search, dur / 2))),
                                   ("end", (max(dur / 2, dur - args.search), dur))):
            exp = (chirp_ms[which] - t0) / 1000 if which in chirp_ms and t0 else None
            win = (exp - args.tol, exp + args.tol) if exp is not None else default_win
            pos, conf = find_chirp(p, sr, *win)
            ok = pos is not None and conf >= args.min_conf
            if not ok:
                warnings.append(f"{key}: {which} chirp NOT found (best score {conf:.3f}, need {args.min_conf}) "
                                f"— too quiet or not played near this device")
            found[which] = (pos if ok else None, conf, exp)
        det[key] = {"frames": n, "sr": sr,
                    "start": found["start"][0], "start_conf": round(found["start"][1], 3),
                    "end": found["end"][0], "end_conf": round(found["end"][1], 3)}
        fmt = lambda w: ("-" if found[w][0] is None else f"{found[w][0] / sr:.2f}s") + f" (score {found[w][1]:.2f}" + \
            ("" if found[w][2] is None else f", expected {found[w][2]:.2f}s") + ")"
        print(f"  {key:8s} start={fmt('start')}  end={fmt('end')}  len={dur:.1f}s")

    r = det[ref]
    if r["start"] is None:
        raise SystemExit("\n".join(["", "WARNINGS:", *[f"  ! {w}" for w in warnings], "",
                                   f"Cannot align: start chirp not found on reference '{ref}'. "
                                   "Try --ref <other device>, or re-record with the chirp audible to every iPad."]))
    sr_out = r["sr"]
    t0_ref = devs[ref].get("first_sample_server_ms")

    # ---- 2. per-device mapping: ref_time = (dev_sample - start_i) / rate_i + start_ref / sr_ref ----
    plan = {}
    for key, d in det.items():
        method = []
        if d["start"] is not None:
            start_i = d["start"]
            method.append("start-chirp")
        else:
            # Fallback: coarse server-clock timestamps (±tens of ms).
            t0 = devs[key].get("first_sample_server_ms")
            if t0 is None or t0_ref is None:
                warnings.append(f"{key}: no start chirp and no timestamps — track skipped")
                print(f"  ! {key}: no chirp and no timestamps — skipped")
                continue
            start_i = r["start"] / r["sr"] * d["sr"] - (t0 - t0_ref) / 1000 * d["sr"]
            method.append("server-clock")
        if d["start"] is not None and d["end"] is not None and r["end"] is not None:
            rate_i = (d["end"] - d["start"]) / ((r["end"] - r["start"]) / r["sr"])  # device samples per ref second
            method.append("end-chirp drift")
        else:
            rate_i = float(d["sr"])
            method.append("nominal rate (no drift fix)")
        # output sample index of this device's sample 0
        out0 = (r["start"] / r["sr"] - start_i / rate_i) * sr_out
        plan[key] = {"rate": rate_i, "out0": out0, "method": " + ".join(method),
                     "drift_ppm": (rate_i / d["sr"] - 1) * 1e6,
                     "out_len": out0 + d["frames"] / rate_i * sr_out}

    # common window: from the latest device start to the earliest device end
    t_lo = max(0.0, max(p["out0"] for p in plan.values()))
    t_hi = min(p["out_len"] for p in plan.values())
    L = int(t_hi - t_lo)

    # ---- 3. resample + shift ----
    out_dir = sdir / "aligned"
    out_dir.mkdir(exist_ok=True)
    for key, p in plan.items():
        d, info = devs[key], sf.info(str(sdir / devs[key]["wav_file"]))
        skip = int(round(t_lo - p["out0"]))  # output samples to drop at the start (may be negative = pad)
        rs = soxr.ResampleStream(p["rate"], sr_out, info.channels, dtype="float32", quality="HQ")
        written = 0
        with sf.SoundFile(str(out_dir / f"{key}.wav"), "w", samplerate=sr_out, channels=info.channels,
                          subtype="PCM_24") as out:
            if skip < 0:
                out.write(np.zeros((-skip, info.channels), np.float32))
                written, skip = -skip, 0
            blocks = sf.blocks(str(sdir / d["wav_file"]), blocksize=BLOCK, dtype="float32", always_2d=True)
            nxt = next(blocks, None)
            while nxt is not None:
                blk, nxt = nxt, next(blocks, None)
                y = rs.resample_chunk(blk, last=nxt is None)
                if skip:
                    drop = min(skip, len(y))
                    y, skip = y[drop:], skip - drop
                y = y[: max(0, L - written)]
                out.write(y)
                written += len(y)
                if written >= L:
                    break
            if written < L:
                out.write(np.zeros((L - written, info.channels), np.float32))
        print(f"  {key:8s} drift {p['drift_ppm']:+7.1f} ppm  shift {(t_lo - p['out0']) / sr_out * 1000:+8.1f} ms  [{p['method']}]")

    # ---- 3b. emotion labels -> aligned timeline ----
    # A label holds from its frame until the next label (or the end). Frames are positions in
    # the device's own recording, so they go through the same offset/drift mapping as the audio.
    total_s = L / sr_out
    emo_rows = []
    for key, p in plan.items():
        evs = sorted(devs[key].get("emotions") or [], key=lambda e: e["frame"])
        t_of = lambda f: (p["out0"] + f / p["rate"] * sr_out - t_lo) / sr_out
        for i, e in enumerate(evs):
            a_s = max(0.0, t_of(e["frame"]))
            b_s = min(total_s, t_of(evs[i + 1]["frame"]) if i + 1 < len(evs) else total_s)
            if b_s > a_s:
                emo_rows.append((key, devs[key].get("speaker_id") or "", a_s, b_s, e["emotion"]))
    if emo_rows:
        with open(out_dir / "emotions.tsv", "w") as f:
            f.write("device\tspeaker_id\tstart_s\tend_s\temotion\n")
            for key, spk, a_s, b_s, emo in sorted(emo_rows, key=lambda r: (r[0], r[2])):
                f.write(f"{key}\t{spk}\t{a_s:.3f}\t{b_s:.3f}\t{emo}\n")
        print(f"Emotion labels: {len(emo_rows)} segments -> {out_dir / 'emotions.tsv'}")

    # ---- 4. metadata + markers on the aligned timeline ----
    start_chirp_aligned = (r["start"] / r["sr"] * sr_out - t_lo) / sr_out
    chirp_srv = next((c["server_ms"] for c in meta.get("chirps", []) if c["label"] == "start" and c["server_ms"]), None)
    with open(out_dir / "markers.tsv", "w") as f:
        f.write("time_s\tlabel\n")
        if chirp_srv is not None:
            for m in meta.get("markers", []):
                if m.get("server_ms"):
                    f.write(f"{start_chirp_aligned + (m['server_ms'] - chirp_srv) / 1000:.3f}\t{m['label']}\n")
    report = {"warnings": warnings, "reference": ref, "sample_rate": sr_out, "length_s": L / sr_out,
              "start_chirp_at_s": start_chirp_aligned, "detections": det,
              "plan": {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                       for k, v in plan.items()}}
    (out_dir / "alignment.json").write_text(json.dumps(report, indent=2))
    print(f"\nAligned {len(plan)} tracks, {L / sr_out:.1f}s each -> {out_dir}")
    if not args.no_combine:
        combine(out_dir, meta)
    if warnings:
        print("\nWARNINGS — check these before trusting the alignment:")
        for w in warnings:
            print("  !", w)
        print("  Tracks without a start chirp used server timestamps only (accuracy roughly 20-100 ms).")


if __name__ == "__main__":
    main()
