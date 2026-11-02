// Sync chirp. MUST match tools/common.py chirp().
export const CHIRP = { f0: 1000, f1: 6000, dur: 0.5 };

function chirpSamples(sr) {
  const n = Math.round(CHIRP.dur * sr);
  const k = (CHIRP.f1 - CHIRP.f0) / CHIRP.dur;
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1)); // np.hanning
    out[i] = w * Math.sin(2 * Math.PI * (CHIRP.f0 * t + 0.5 * k * t * t));
  }
  return out;
}

// WAV blob URL: [preSilence s][chirp][postSilence s]. Played through an <audio>
// element so it is audible even when the phone's silent switch is on.
export function chirpWavUrl(preSilence, postSilence, sr = 48000, gain = 0.9) {
  const c = chirpSamples(sr);
  const pre = Math.round(preSilence * sr), post = Math.round(postSilence * sr);
  const n = pre + c.length + post;
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < c.length; i++) v.setInt16(44 + (pre + i) * 2, Math.round(c[i] * gain * 32767), true);
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
}
