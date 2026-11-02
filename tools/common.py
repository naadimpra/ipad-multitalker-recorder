"""Shared helpers: sync-chirp definition and raw -> WAV conversion.

The chirp formula MUST stay identical to static/chirp.js.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

CHIRP_F0 = 1000.0   # Hz
CHIRP_F1 = 6000.0   # Hz
CHIRP_DUR = 0.5     # seconds


def chirp(sr: int) -> np.ndarray:
    """Linear sweep CHIRP_F0 -> CHIRP_F1 with a Hann window."""
    n = int(round(CHIRP_DUR * sr))
    t = np.arange(n) / sr
    k = (CHIRP_F1 - CHIRP_F0) / CHIRP_DUR
    x = np.sin(2 * np.pi * (CHIRP_F0 * t + 0.5 * k * t * t))
    return (x * np.hanning(n)).astype(np.float32)


def raw_to_wav(raw_path: Path, wav_path: Path, sr: int, channels: int,
               subtype: str = "PCM_24", block_frames: int = 48000 * 10) -> int:
    """Convert interleaved float32 .f32 file to WAV in blocks. Returns frame count."""
    frame_bytes = 4 * channels
    total = raw_path.stat().st_size // frame_bytes
    with open(raw_path, "rb") as f, sf.SoundFile(wav_path, "w", samplerate=sr,
                                                 channels=channels, subtype=subtype) as out:
        left = total
        while left > 0:
            n = min(block_frames, left)
            buf = np.frombuffer(f.read(n * frame_bytes), dtype="<f4")
            out.write(buf.reshape(-1, channels))
            left -= n
    return total


def load_session(session_dir: Path) -> dict:
    return json.loads((session_dir / "session.json").read_text())


def finalize_session(session_dir: Path) -> None:
    """(Re)build WAVs from raw files — use after a server crash."""
    meta = load_session(session_dir)
    for key, dev in meta["devices"].items():
        raw = session_dir / dev["raw_file"]
        if not raw.exists():
            continue
        wav = session_dir / dev["wav_file"]
        frames = raw_to_wav(raw, wav, dev["sample_rate"], dev["channels"])
        dev["frames"] = frames
        print(f"{key}: {frames / dev['sample_rate']:.1f}s -> {wav.name}")
    (session_dir / "session.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    for d in sys.argv[1:]:
        finalize_session(Path(d))
