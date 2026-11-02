"""Merge the aligned per-device WAVs into single files.

    python -m tools.combine recordings/<session_id> [--devices spk1,spk2,spk3,spk4]

Writes into aligned/:
  combined.wav        one multichannel WAV, one channel per device (sample-aligned)
  combined.json       which channel is which device / participant
  mix_mono.wav        mono sum of those channels (peak-limited so it never clips)

Default devices: the speaker iPads (spk1, spk2, ...). Add the room mic with
--devices spk1,spk2,spk3,spk4,center. (tools.align runs this automatically.)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from tools.common import load_session

BLOCK = 48000 * 10
TARGET_PEAK = 0.89  # about -1 dBFS


def default_devices(adir: Path, meta: dict) -> list[str]:
    have = [k for k in sorted(meta["devices"]) if (adir / f"{k}.wav").exists()]
    spk = [k for k in have if k not in ("center", "lav")]
    return spk if len(spk) >= 2 else have


def combine(adir: Path, meta: dict, devices: list[str] | None = None) -> dict | None:
    devices = devices or default_devices(adir, meta)
    if len(devices) < 2:
        print("Combine: fewer than 2 aligned devices, nothing to merge.")
        return None
    files = [sf.SoundFile(str(adir / f"{k}.wav")) for k in devices]
    try:
        sr = files[0].samplerate
        if any(f.samplerate != sr for f in files):
            raise SystemExit("Aligned files have different sample rates; re-run tools.align.")
        frames = min(len(f) for f in files)  # align makes them equal; guard anyway
        chans, channel_map = 0, []
        for k, f in zip(devices, files):
            for c in range(f.channels):
                channel_map.append({"index": chans, "device": k, "device_channel": c,
                                    "speaker_id": meta["devices"].get(k, {}).get("speaker_id", "")})
                chans += 1

        def blocks():
            for f in files:
                f.seek(0)
            left = frames
            while left > 0:
                n = min(BLOCK, left)
                yield np.hstack([f.read(n, dtype="float32", always_2d=True) for f in files])
                left -= n

        peak = 0.0
        with sf.SoundFile(str(adir / "combined.wav"), "w", samplerate=sr, channels=chans, subtype="PCM_24") as out:
            for blk in blocks():
                out.write(blk)
                peak = max(peak, float(np.abs(blk.sum(axis=1)).max()))
        gain = min(1.0, TARGET_PEAK / peak) if peak > 0 else 1.0
        with sf.SoundFile(str(adir / "mix_mono.wav"), "w", samplerate=sr, channels=1, subtype="PCM_24") as out:
            for blk in blocks():
                out.write((blk.sum(axis=1, keepdims=True) * gain).astype(np.float32))
    finally:
        for f in files:
            f.close()

    info = {"file": "combined.wav", "sample_rate": sr, "frames": frames, "duration_s": frames / sr,
            "channels": channel_map, "mono_mix_file": "mix_mono.wav", "mono_mix_gain": round(gain, 4)}
    (adir / "combined.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
    print(f"Combined {len(devices)} devices -> {adir / 'combined.wav'} ({chans} ch, {frames / sr:.1f}s) "
          f"+ mix_mono.wav (gain x{gain:.2f})")
    for c in channel_map:
        print(f"  channel {c['index'] + 1}: {c['device']}" + (f" ({c['speaker_id']})" if c["speaker_id"] else ""))
    return info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--devices", default=None, help="comma-separated device labels, in channel order")
    args = ap.parse_args()
    adir = args.session / "aligned"
    if not adir.exists():
        raise SystemExit("Run python -m tools.align first.")
    combine(adir, load_session(args.session), args.devices.split(",") if args.devices else None)


if __name__ == "__main__":
    main()
