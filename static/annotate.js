// Transcription page: one waveform lane per device, speech segments on the lanes, and for every
// segment four lines (as written / yomigana / pitch accent / romaji). Everything is saved as you type.
import { EMOTIONS } from "/static/emotions.js";

const $ = (id) => document.getElementById(id);
const EMO = Object.fromEntries(EMOTIONS.map((e) => [e.id, e]));
const COLORS = ["#3b82f6", "#ec4899", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4", "#ef4444", "#84cc16"];
const FIELDS = [
  { key: "text", tag: "漢字", ph: "What was said, as written (漢字かな交じり)", lang: "ja" },
  { key: "kana", tag: "よみ", ph: "よみがな (ひらがな, a space between words)", lang: "ja" },
  { key: "romaji", tag: "Romaji", ph: "romaji", lang: "ja-Latn" },
];
// What does not belong in a reading line: kanji, digits or Latin letters left in the yomigana;
// anything that is not plain Latin in the romaji. Shown as a warning, never blocked.
const SUSPECT = { kana: /[㐀-鿿々〆A-Za-z0-9０-９]/, romaji: /[^\x00-\x7fāīūēōâîûêô]/ };
const RULER = 24, MAX_PPS = 600, MIN_SEG = 0.05, EDGE_PX = 6;

// ---- morae and pitch ----
// The pitch line has one mark per mora of the yomigana: L low, H high, D devoiced (and low), _ not set.
// Devoicing is one fact written three ways, kept in step: ＊ after the mora in the yomigana, D in the
// pitch line, a capital vowel in the romaji (し＊ / D / shI).
const SMALL_KANA = "ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ", ALONE = "んっーンッ";
const isKana = (c) => (c >= "ぁ" && c <= "ゖ") || (c >= "ァ" && c <= "ヺ") || c === "ー";
const PITCH_NAME = { L: "low", H: "high", D: "devoiced, low", _: "not set" };

// [{ k: "きょ", end: index just after the mora (and its ＊), dev: has ＊, word: which space-separated word }]
function morae(kana) {
  const out = [];
  let word = 0, gap = false;
  for (let i = 0; i < kana.length; i++) {
    const c = kana[i], last = out[out.length - 1], touching = last && last.end === i;
    if (/\s/.test(c)) { gap = true; continue; }
    if (gap && out.length) word++;
    gap = false;
    if (c === "＊" || c === "*") {
      if (touching && !last.dev) { last.dev = true; last.end = i + 1; }
    } else if (!isKana(c)) {
      continue;  // punctuation, or something that should not be in a reading
    } else if (SMALL_KANA.includes(c) && touching && !last.dev && !ALONE.includes(last.k)) {
      last.k += c;  // きょ, てぃ: one mora
      last.end = i + 1;
    } else {
      out.push({ k: c, end: i + 1, dev: false, word });
    }
  }
  return out;
}
function pitchOf(s, M = morae(s.kana)) {
  const p = [...s.pitch.replace(/\s/g, "")];
  return M.map((m, i) => ("LHD".includes(p[i] || "?") ? p[i] : "_"));
}
function formatPitch(M, p) {  // grouped like the words of the yomigana
  let out = "";
  M.forEach((m, i) => { out += (i && m.word !== M[i - 1].word ? " " : "") + p[i]; });
  return out;
}
// New yomigana: morae that did not change (from the front and from the back) keep their pitch mark.
function setKana(s, kana) {
  const oldM = morae(s.kana), oldP = pitchOf(s, oldM), M = morae(kana);
  let a = 0, b = 0;
  while (a < oldM.length && a < M.length && oldM[a].k === M[a].k) a++;
  while (b < oldM.length - a && b < M.length - a && oldM[oldM.length - 1 - b].k === M[M.length - 1 - b].k) b++;
  const p = M.map((m, i) => (i < a ? oldP[i] : i >= M.length - b ? oldP[oldM.length - (M.length - i)] : "_"));
  M.forEach((m, i) => { if (m.dev) p[i] = "D"; else if (p[i] === "D") p[i] = "L"; });
  s.kana = kana;
  s.pitch = formatPitch(M, p);
}
const FONT = '-apple-system, "Hiragino Sans", system-ui, sans-serif';

const sid = new URLSearchParams(location.search).get("s");
const api = (p = "") => `/api/annotate/s/${encodeURIComponent(sid)}${p}`;
const JSON_HEADERS = { "content-type": "application/json" };

let info, tracks = [], segs = [], duration = 0.1, rev = 0;
let pps = 50, laneH = 96, t = 0;       // zoom (px per second), lane height (px), playhead (s)
let sel = null, active = null, listen = "track", filter = null, showEmo = false;
let drag = null, undo = [], textUndoFor = null;

const cv = $("cv"), ctx = cv.getContext("2d"), scroller = $("scroller"), spacer = $("spacer"), list = $("list");
let W = 0, H = 0, css = {};

// ---------------- small helpers ----------------
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const newId = () => Math.random().toString(16).slice(2, 10).padEnd(8, "0");
const newSeg = (device, start, end) => ({ id: newId(), device, start, end, text: "", kana: "", pitch: "", romaji: "" });
const segById = (id) => segs.find((s) => s.id === id);
const trackOf = (dev) => tracks.find((x) => x.device === dev);
const sortSegs = () => segs.sort((a, b) => a.start - b.start || a.device.localeCompare(b.device));
const shownSegs = () => segs.filter((s) => !filter || s.device === filter);
const rowOf = (id) => (id ? list.querySelector(`.an-row[data-id="${CSS.escape(id)}"]`) : null);
const fieldEl = (id, f) => rowOf(id)?.querySelector(`textarea[data-f="${f}"]`);

function fmt(x, digits = 1) {
  const u = Math.round(Math.max(0, x) * 10 ** digits), per = 60 * 10 ** digits;
  const m = Math.floor(u / per), s = ((u % per) / 10 ** digits).toFixed(digits).padStart(digits ? digits + 3 : 2, "0");
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function toast(text, kind = "info", ms = 4000) {
  const d = document.createElement("div");
  d.className = `msg ${kind}`;
  d.textContent = text;
  $("msgs").append(d);
  setTimeout(() => d.remove(), ms);
}

function readTheme() {
  const s = getComputedStyle(document.documentElement);
  for (const k of ["card", "fg", "muted", "line", "soft", "warn"]) css[k] = s.getPropertyValue(`--${k}`).trim();
}

// ---------------- session list ----------------
async function showPicker(error) {
  $("picker").hidden = false;
  if (error) toast(error, "err", 6000);
  const all = await (await fetch("/api/annotate/sessions")).json();
  $("noSessions").hidden = all.length > 0;
  $("sessions").replaceChildren(...all.map((s) => {
    const a = document.createElement("a");
    a.className = "card an-sess";
    a.href = `/annotate?s=${encodeURIComponent(s.id)}`;
    a.innerHTML = '<div class="grow"><b></b><div class="muted"></div></div><span class="flag"></span>';
    a.querySelector("b").textContent = s.name;
    const when = s.started_at_ms ? new Date(s.started_at_ms).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
    a.querySelector(".muted").textContent = [when, s.duration != null ? fmt(s.duration, 0) : "", s.tracks.join(", "),
      s.timeline === "aligned" ? "aligned" : ""].filter(Boolean).join(" · ");
    const flag = a.querySelector(".flag");
    flag.textContent = s.segments ? `${s.done} / ${s.segments}` : "not started";
    flag.classList.toggle("ok", s.segments > 0 && s.done === s.segments);
    return a;
  }));
}

// ---------------- open a session ----------------
async function openSession() {
  const r = await fetch(api());
  if (!r.ok) return showPicker(`Session "${sid}" was not found.`);
  info = await r.json();
  document.title = `${info.name} · Transcribe`;
  $("title").textContent = info.name;
  $("editor").hidden = false;
  readTheme();

  tracks = info.tracks.map((x, i) => ({ ...x, color: COLORS[i % COLORS.length], mips: null }));
  segs = info.transcript.segments.filter((s) => trackOf(s.device)).map((s) => ({
    id: s.id || newId(), device: s.device, start: +s.start, end: +s.end,
    text: s.text || "", kana: s.kana || "", pitch: s.pitch || "", romaji: s.romaji || "" }));
  sortSegs();
  rev = info.transcript.rev;
  duration = Math.max(0.1, ...tracks.map((x) => x.duration));
  active = tracks[0]?.device ?? null;
  showEmo = tracks.some((x) => x.emotions.some((e) => e[2] !== "neutral"));
  laneH = tracks.length <= 2 ? 120 : tracks.length <= 4 ? 92 : 72;

  if (!tracks.length) banner("No audio was found in this session.");
  else if (info.timeline === "raw" && tracks.length > 1) {
    banner("These tracks are not aligned yet, so each one starts at its own zero. Run tools.align first: " +
      "a transcript made now stays on the unaligned tracks.");
  }
  if (!info.readings) {
    $("hint").hidden = false;
    $("hint").textContent = "よみがな is not drafted automatically on this machine (romaji still is). " +
      "To turn it on: pip install fugashi unidic-lite, then restart.";
  }
  $("listen").hidden = !info.mix;

  $("gutter").replaceChildren(...tracks.map((tr) => {
    const b = document.createElement("button");
    b.className = "an-track";
    b.dataset.device = tr.device;
    b.title = "Listen to this track";
    b.style.height = `${laneH}px`;
    b.style.setProperty("--c", tr.color);
    b.innerHTML = "<i></i><span><b></b><small></small></span>";
    b.querySelector("b").textContent = tr.speaker;
    b.querySelector("small").textContent = tr.speaker !== tr.device ? tr.device : "";
    b.onclick = () => setActive(tr.device);
    return b;
  }));
  if (tracks.length > 1) {
    $("filter").hidden = false;
    $("filter").replaceChildren(...[null, ...tracks].map((tr) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.textContent = tr ? tr.speaker : "All speakers";
      b.onclick = () => { filter = tr ? tr.device : null; renderList(); };
      b.dataset.device = tr ? tr.device : "";
      return b;
    }));
  }

  layout();
  setZoom(Math.max(W / duration, 50));
  setActive(active);
  renderList();
  tracks.forEach(loadPeaks);
  new ResizeObserver(() => { layout(); setZoom(pps); }).observe(scroller);
  requestAnimationFrame(tick);
}

function banner(text) {
  $("banner").hidden = false;
  $("banner").textContent = text;
}

// The server sends (min, max) per ~5 ms. Halve it repeatedly so any zoom level reads ~1 value per pixel.
async function loadPeaks(tr) {
  const buf = new Int8Array(await (await fetch(tr.peaks)).arrayBuffer());
  let n = buf.length >> 1, lo = new Int8Array(n), hi = new Int8Array(n);
  for (let i = 0; i < n; i++) { lo[i] = buf[2 * i]; hi[i] = buf[2 * i + 1]; }
  const mips = [{ lo, hi, n }];
  while (n > 2048) {
    const m = Math.ceil(n / 2), a = new Int8Array(m), b = new Int8Array(m);
    for (let i = 0; i < m; i++) {
      const j = 2 * i, k = Math.min(j + 1, n - 1);
      a[i] = Math.min(lo[j], lo[k]);
      b[i] = Math.max(hi[j], hi[k]);
    }
    lo = a; hi = b; n = m;
    mips.push({ lo, hi, n });
  }
  tr.mips = mips;
  draw();
}

// ---------------- timeline: layout, zoom, painting ----------------
function layout() {
  W = scroller.clientWidth;
  H = RULER + tracks.length * laneH;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(W * dpr);
  cv.height = Math.round(H * dpr);
  cv.style.width = `${W}px`;
  cv.style.height = `${H}px`;
  spacer.style.height = `${H}px`;
}

function setZoom(next, anchorX = W / 2) {
  const fit = W / duration;  // never zoom out further than the whole recording
  next = fit >= MAX_PPS ? fit : clamp(next, fit, MAX_PPS);
  const at = (scroller.scrollLeft + anchorX) / pps;
  pps = next;
  spacer.style.width = `${Math.max(W, Math.ceil(duration * pps))}px`;
  scroller.scrollLeft = at * pps - anchorX;
  placePlayhead(true);
  draw();
}

let drawQueued = false;
function draw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; paint(); });
}

function paint() {
  if (!info) return;
  const dpr = window.devicePixelRatio || 1, x0 = scroller.scrollLeft;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.textBaseline = "middle";

  // ruler
  ctx.fillStyle = css.soft;
  ctx.fillRect(0, 0, W, RULER);
  const step = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800].find((s) => s * pps >= 72) || 3600;
  ctx.font = `11px ${FONT}`;
  ctx.fillStyle = css.muted;
  for (let k = Math.floor(x0 / pps / step); k * step <= (x0 + W) / pps; k++) {
    const x = Math.round(k * step * pps - x0);
    ctx.fillRect(x, RULER - 8, 1, 8);
    ctx.fillText(fmt(k * step, step < 1 ? 1 : 0), x + 4, RULER / 2 - 1);
  }

  tracks.forEach((tr, i) => paintLane(tr, RULER + i * laneH, x0));

  // admin markers and sync chirps
  for (const m of info.marks) {
    const x = Math.round(m.t * pps - x0);
    if (x < -80 || x > W) continue;
    ctx.fillStyle = m.kind === "chirp" ? css.muted : css.warn;
    ctx.globalAlpha = 0.6;
    ctx.fillRect(x, RULER, 1, H - RULER);
    ctx.globalAlpha = 1;
    ctx.fillText(m.kind === "chirp" ? "♪ chirp" : `⚑ ${m.label}`, x + 4, H - 8);
  }
}

function paintLane(tr, y, x0) {
  const mid = y + laneH / 2, amp = laneH / 2 - 8;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, y, W, laneH);
  ctx.clip();
  ctx.fillStyle = css.line;
  ctx.fillRect(0, y, W, 1);

  if (tr.mips) {
    const bins0 = tr.peaks_rate / pps;  // finest-level values per pixel
    let k = 0;
    while (k + 1 < tr.mips.length && bins0 / 2 ** (k + 1) >= 1) k++;
    const m = tr.mips[k], bpp = bins0 / 2 ** k;
    ctx.fillStyle = tr.color;
    ctx.globalAlpha = tr.device === active ? 1 : 0.55;
    for (let x = 0; x < W; x++) {
      const a = Math.floor((x0 + x) * bpp);
      if (a >= m.n) break;
      const b = Math.min(m.n, Math.max(a + 1, Math.floor((x0 + x + 1) * bpp)));
      let lo = 127, hi = -128;
      for (let j = a; j < b; j++) { if (m.lo[j] < lo) lo = m.lo[j]; if (m.hi[j] > hi) hi = m.hi[j]; }
      const y1 = mid - (hi / 127) * amp, y2 = mid - (lo / 127) * amp;
      ctx.fillRect(x, y1, 1, Math.max(1, y2 - y1));
    }
    ctx.globalAlpha = 1;
  }

  if (showEmo) {  // what the participant had tapped, as a strip along the bottom of the lane
    for (const [a, b, emo] of tr.emotions) {
      const xa = a * pps - x0, xb = b * pps - x0;
      if (xb < 0 || xa > W) continue;
      ctx.fillStyle = EMO[emo]?.color || css.muted;
      ctx.fillRect(xa, y + laneH - 4, xb - xa, 4);
    }
  }

  for (const s of segs) {
    if (s.device === tr.device) paintSeg(tr, y, s.start * pps - x0, s.end * pps - x0, s.id === sel, s.text);
  }
  if (drag?.kind === "new" && drag.ghost && drag.tr === tr) {
    paintSeg(tr, y, drag.ghost[0] * pps - x0, drag.ghost[1] * pps - x0, true, "");
  }
  ctx.restore();
}

function paintSeg(tr, y, xa, xb, on, text) {
  if (xb < 0 || xa > W) return;
  const empty = !text.trim();
  ctx.fillStyle = tr.color;
  ctx.globalAlpha = on ? 0.3 : empty ? 0.07 : 0.16;
  ctx.fillRect(xa, y + 1, xb - xa, laneH - 1);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = on ? css.fg : tr.color;
  ctx.lineWidth = on ? 2 : 1;
  ctx.setLineDash(empty && !on ? [4, 3] : []);  // dashed = nothing typed yet
  ctx.strokeRect(xa + 0.5, y + 1.5, Math.max(1, xb - xa - 1), laneH - 2);
  ctx.setLineDash([]);
  const left = Math.max(xa, 0);
  if (!empty && xb - left > 28) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(left + 2, y + 3, xb - left - 4, 18);
    ctx.clip();
    ctx.fillStyle = css.card;
    ctx.globalAlpha = 0.82;
    ctx.fillRect(left + 2, y + 3, xb - left - 4, 18);
    ctx.globalAlpha = 1;
    ctx.fillStyle = css.fg;
    ctx.font = `12px ${FONT}`;
    ctx.fillText(text, left + 6, y + 12.5);
    ctx.restore();
  }
}

let lastPh = -1, lastClock = "";
function placePlayhead(force) {
  const x = Math.round(t * pps * 2) / 2;
  if (force || x !== lastPh) {
    lastPh = x;
    $("ph").style.transform = `translateX(${x}px)`;
  }
  const c = `${fmt(t)} / ${fmt(duration)}`;
  if (c !== lastClock) $("clock").textContent = lastClock = c;
}

// ---------------- audio ----------------
const audio = new Audio();
audio.preload = "auto";
let srcUrl = "", pendingSeek = null, stopAt = null, loopFrom = 0;

function setSource() {
  const url = listen === "mix" && info.mix ? info.mix : trackOf(active)?.audio;
  if (!url || url === srcUrl) return;
  const wasPlaying = !audio.paused;
  srcUrl = url;
  audio.src = url;
  audio.defaultPlaybackRate = audio.playbackRate = +$("speed").value;
  pendingSeek = t;  // every track shares the timeline: carry on from the same moment
  if (wasPlaying) audio.play().catch(() => {});
}
audio.addEventListener("loadedmetadata", () => {
  if (pendingSeek != null) audio.currentTime = pendingSeek;
  pendingSeek = null;
});
audio.addEventListener("play", () => { $("play").textContent = "❚❚"; });
audio.addEventListener("pause", () => { $("play").textContent = "▶"; });
audio.addEventListener("error", () => { if (srcUrl) toast("This track could not be played.", "err"); });

function seek(x) {
  t = clamp(x, 0, duration);
  if (audio.readyState >= 1) audio.currentTime = t; else pendingSeek = t;
  placePlayhead();
}
function play() { audio.play().catch(() => {}); }
function toggle() {
  if (!audio.paused) return audio.pause();
  stopAt = null;
  if (t >= duration - 0.05) seek(0);
  play();
}
function playSeg(s, from = s.start) {
  setActive(s.device);
  stopAt = s.end;
  loopFrom = s.start;
  seek(from);
  play();
}
function toggleSeg(s) {  // pause, or hear it again: back up a little so the last words repeat
  if (!audio.paused) return audio.pause();
  const inside = active === s.device && t > s.start && t < s.end - 0.1;
  playSeg(s, inside ? Math.max(s.start, t - 1.5) : s.start);
}

function setActive(dev) {
  active = dev;
  for (const b of $("gutter").children) b.classList.toggle("on", b.dataset.device === dev);
  setSource();
  draw();
}

let nowId = null;
function tick() {
  if (!audio.paused && audio.readyState >= 1 && pendingSeek == null && !audio.seeking) {
    t = audio.currentTime;
    if (stopAt != null && t >= stopAt) {
      if ($("loop").checked) { audio.currentTime = t = loopFrom; } else { audio.pause(); t = stopAt; stopAt = null; }
    }
    const x = t * pps - scroller.scrollLeft;  // keep the playhead in view
    if (!drag && (x > W - 30 || x < 0)) scroller.scrollLeft = t * pps - W * 0.12;
  }
  placePlayhead();
  const cur = segs.find((s) => t >= s.start && t < s.end && (listen === "mix" || s.device === active))?.id ?? null;
  if (cur !== nowId) {
    rowOf(nowId)?.classList.remove("now");
    rowOf(nowId = cur)?.classList.add("now");
  }
  requestAnimationFrame(tick);
}

// ---------------- waveform interaction ----------------
function timeAt(e) {
  return clamp((scroller.scrollLeft + e.clientX - cv.getBoundingClientRect().left) / pps, 0, duration);
}

function hit(e) {
  const r = cv.getBoundingClientRect(), y = e.clientY - r.top, tt = timeAt(e);
  if (y < RULER) return { zone: "ruler", t: tt };
  const tr = tracks[Math.floor((y - RULER) / laneH)];
  if (!tr) return null;
  const tol = EDGE_PX / pps;
  let edge = null, inside = null;
  for (const s of segs) {
    if (s.device !== tr.device) continue;
    const within = tt >= s.start && tt <= s.end;
    if (within) inside = s;
    for (const which of ["start", "end"]) {
      const d = Math.abs(tt - s[which]);
      // where two segments touch, the one under the cursor wins
      if (d <= tol && (!edge || (within && !edge.within) || (within === edge.within && d < edge.d))) {
        edge = { seg: s, which, d, within };
      }
    }
  }
  if (edge) return { zone: "edge", seg: edge.seg, edge: edge.which, t: tt, tr };
  if (inside) return { zone: "seg", seg: inside, t: tt, tr };
  return { zone: "lane", t: tt, tr };
}

// Free room on a track around [a, b]: a speaker's segments never overlap each other.
function room(dev, a, b, skip) {
  let lo = 0, hi = trackOf(dev).duration;
  for (const s of segs) {
    if (s.device !== dev || s === skip) continue;
    if (s.end <= a + 1e-6) lo = Math.max(lo, s.end);
    else if (s.start >= b - 1e-6) hi = Math.min(hi, s.start);
  }
  return [lo, hi];
}

cv.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  const h = hit(e);
  if (!h) return;
  cv.setPointerCapture(e.pointerId);
  if (h.zone === "ruler") {
    drag = { kind: "scrub" };
    stopAt = null;
    seek(h.t);
  } else if (h.zone === "edge") {
    pushUndo();
    select(h.seg.id, { reveal: false });
    const [lo, hi] = room(h.seg.device, h.seg.start, h.seg.end, h.seg);
    drag = { kind: "edge", seg: h.seg, edge: h.edge, lo, hi, moved: false };
  } else if (h.zone === "seg") {
    select(h.seg.id, { reveal: false });
    stopAt = null;
    seek(h.t);
  } else {
    const [lo, hi] = room(h.tr.device, h.t, h.t);
    drag = { kind: "new", tr: h.tr, t0: h.t, x: e.clientX, lo, hi, ghost: null };
  }
});

cv.addEventListener("pointermove", (e) => {
  if (!drag) {
    const z = hit(e)?.zone;
    cv.style.cursor = z === "edge" ? "ew-resize" : z === "seg" ? "pointer" : z === "lane" ? "crosshair" : "default";
    return;
  }
  const tt = timeAt(e);
  if (drag.kind === "scrub") {
    seek(tt);
  } else if (drag.kind === "edge") {
    const s = drag.seg;
    if (drag.edge === "start") s.start = clamp(tt, drag.lo, s.end - MIN_SEG);
    else s.end = clamp(tt, s.start + MIN_SEG, drag.hi);
    drag.moved = true;
    const row = rowOf(s.id);
    if (row) setTime(row, s);
    draw();
  } else if (Math.abs(e.clientX - drag.x) > 4 || drag.ghost) {
    drag.ghost = [clamp(Math.min(drag.t0, tt), drag.lo, drag.hi), clamp(Math.max(drag.t0, tt), drag.lo, drag.hi)];
    draw();
  }
});

function endDrag() {
  const d = drag;
  drag = null;
  if (!d) return;
  if (d.kind === "edge") {
    if (!d.moved) { undo.pop(); $("undoBtn").disabled = !undo.length; return; }
    d.seg.start = +d.seg.start.toFixed(3);
    d.seg.end = +d.seg.end.toFixed(3);
    changed();
  } else if (d.kind === "new") {
    if (d.ghost && d.ghost[1] - d.ghost[0] >= 0.1) {
      pushUndo();
      const s = newSeg(d.tr.device, +d.ghost[0].toFixed(3), +d.ghost[1].toFixed(3));
      segs.push(s);
      if (filter && filter !== s.device) filter = null;
      changed();
      select(s.id);
      fieldEl(s.id, "text")?.focus();
    } else {  // a plain click on empty space: go there
      setActive(d.tr.device);
      stopAt = null;
      seek(d.t0);
      select(null);
    }
  }
  draw();
}
cv.addEventListener("pointerup", endDrag);
cv.addEventListener("pointercancel", endDrag);

cv.addEventListener("dblclick", (e) => {
  const h = hit(e);
  if (h?.zone !== "seg" && h?.zone !== "edge") return;
  select(h.seg.id);
  fieldEl(h.seg.id, "text")?.focus();
  playSeg(h.seg);
});

scroller.addEventListener("scroll", draw, { passive: true });
scroller.addEventListener("wheel", (e) => {
  if (e.ctrlKey || e.metaKey) {  // pinch, or Ctrl/⌘ + wheel: zoom around the cursor
    e.preventDefault();
    setZoom(pps * Math.exp(-e.deltaY * 0.01), e.clientX - cv.getBoundingClientRect().left);
  } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {  // a plain mouse wheel scrolls along the recording
    const tl = scroller.parentElement;
    if (tl.scrollHeight <= tl.clientHeight) {
      e.preventDefault();
      scroller.scrollLeft += e.deltaY;
    }
  }
}, { passive: false });

// ---------------- segment list ----------------
const FIELD_SIZING = CSS.supports("field-sizing", "content");
function grow(ta) {
  if (FIELD_SIZING || !ta.offsetParent) return;
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight + 2}px`;
}

function setTime(row, s) {
  row.querySelector(".an-time").innerHTML = `${fmt(s.start)} – ${fmt(s.end)} <small>${(s.end - s.start).toFixed(1)} s</small>`;
}

function emotionOf(s) {  // the emotion tapped during most of the segment
  let best = null, most = 0;
  for (const [a, b, emo] of trackOf(s.device).emotions) {
    const ov = Math.min(s.end, b) - Math.max(s.start, a);
    if (ov > most) { most = ov; best = emo; }
  }
  return best;
}

function check(label, key, value) {
  const bad = SUSPECT[key]?.test(value);
  label.classList.toggle("warn", !!bad);
  label.title = !bad ? "" : key === "kana" ? "There are kanji, digits or Latin letters left in the reading"
    : "There are non-Latin characters left in the romaji";
}

function rowEl(s) {
  const tr = trackOf(s.device), row = document.createElement("div");
  row.className = "an-row";
  row.dataset.id = s.id;
  row.style.setProperty("--c", tr.color);
  row.innerHTML = `
    <div class="an-meta">
      <button class="an-rowplay" data-act="play" tabindex="-1" title="Play this segment">▶</button>
      <span class="an-time"></span><span class="an-who"></span><span class="an-emo"></span>
    </div>
    <div class="an-fields"></div>
    <div class="an-acts">
      <button data-act="read" tabindex="-1" title="Write the yomigana and romaji again from the text">↻ よみ</button>
      <button data-act="split" tabindex="-1" title="Cut at the playhead (S). With the cursor in the text, the text is cut there too.">Split</button>
      <button data-act="merge" tabindex="-1" title="Join with this speaker's next segment (M)">Merge ↓</button>
      <button data-act="del" class="del" tabindex="-1" title="Delete this segment (Delete)">Delete</button>
    </div>`;
  setTime(row, s);
  if (tracks.length > 1) row.querySelector(".an-who").textContent = tr.speaker;
  const emo = showEmo && EMO[emotionOf(s)];
  if (emo) {
    row.querySelector(".an-emo").textContent = emo.emoji;
    row.querySelector(".an-emo").title = emo.en;
  }
  for (const f of FIELDS) {
    const label = document.createElement("label");
    label.className = "an-field";
    label.dataset.f = f.key;
    const tag = document.createElement("span");
    tag.textContent = f.tag;
    const ta = document.createElement("textarea");
    Object.assign(ta, { rows: 1, value: s[f.key], placeholder: f.ph, lang: f.lang, spellcheck: false, autocapitalize: "off" });
    ta.dataset.f = f.key;
    label.append(tag, ta);
    check(label, f.key, s[f.key]);
    row.querySelector(".an-fields").append(label);
    if (f.key === "kana") {  // the pitch line sits right under the yomigana it belongs to
      const line = document.createElement("div");
      line.className = "an-field";
      line.dataset.f = "pitch";
      line.innerHTML = '<span>高低</span><div class="an-pitch" role="group" aria-label="Pitch accent, one mark per mora"></div>';
      row.querySelector(".an-fields").append(line);
    }
  }
  renderPitch(row, s);
  return row;
}

// One box per mora, grouped by word. H sits high and L / D sit low, so the row reads as the pitch contour.
function renderPitch(row, s) {
  const box = row.querySelector(".an-pitch"), M = morae(s.kana), p = pitchOf(s, M);
  const had = box.contains(document.activeElement) ? +document.activeElement.dataset.i : -1;
  if (!M.length) {
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = "One box per mora appears here once the よみ is filled in.";
    box.replaceChildren(hint);
    return;
  }
  const words = [];
  M.forEach((m, i) => {
    if (!i || m.word !== M[i - 1].word) {
      words.push(document.createElement("span"));
      words[words.length - 1].className = "an-word";
    }
    const b = document.createElement("button");
    b.className = "an-mora";
    b.dataset.i = i;
    b.dataset.p = p[i];
    b.tabIndex = i ? -1 : 0;  // Tab reaches the line once; the arrow keys move inside it
    b.title = "Click to change, or type L (low), H (high) or D (devoiced)";
    b.setAttribute("aria-label", `${m.k}: ${PITCH_NAME[p[i]]}`);
    b.innerHTML = "<small></small><b></b>";
    b.firstChild.textContent = m.k;
    b.lastChild.textContent = p[i] === "_" ? "·" : p[i];
    words[words.length - 1].append(b);
  });
  box.replaceChildren(...words);
  if (had >= 0) box.querySelector(`[data-i="${Math.min(had, M.length - 1)}"]`)?.focus();
}

function setPitch(s, i, mark) {
  const M = morae(s.kana), p = pitchOf(s, M), row = rowOf(s.id);
  if (!M[i] || p[i] === mark) return;
  if (textUndoFor !== s.id + "pitch") { pushUndo(); textUndoFor = s.id + "pitch"; }
  const before = s.kana;
  let kana = before;
  if (mark === "D" && !M[i].dev) kana = kana.slice(0, M[i].end) + "＊" + kana.slice(M[i].end);
  else if (mark !== "D" && M[i].dev) kana = kana.slice(0, M[i].end - 1) + kana.slice(M[i].end);
  p[i] = mark;
  s.kana = kana;
  s.pitch = formatPitch(morae(kana), p);
  if (kana !== before) {  // devoicing changed: the ＊ in the yomigana and the romaji follow
    const ta = fieldEl(s.id, "kana");
    if (ta) { ta.value = kana; ta._prev = kana; grow(ta); }
    draftReading(s.id, "kana", before);
  }
  if (row) renderPitch(row, s);
  markDirty();
}

function focusLine(id, f) {
  (f === "pitch" ? rowOf(id)?.querySelector(".an-mora") : fieldEl(id, f))?.focus();
}

function renderList() {
  const el = document.activeElement, focusRow = el?.closest?.(".an-row");
  const focus = focusRow && el.dataset.f ? { id: focusRow.dataset.id, f: el.dataset.f, pos: el.selectionStart } : null;
  const shown = shownSegs();
  if (shown.length) {
    list.replaceChildren(...shown.map(rowEl));
  } else {
    const d = document.createElement("div");
    d.className = "an-empty";
    d.innerHTML = segs.length ? "<p>No segments for this speaker yet.</p>" : `
      <p><b>No segments yet.</b><br>Auto-segment marks the speech in each track for you. You can also drag on a
      waveform to mark a segment by hand. Then type what was said.</p>
      <button data-act="draft">Auto-segment</button>`;
    list.replaceChildren(d);
  }
  for (const ta of list.querySelectorAll("textarea")) grow(ta);
  rowOf(sel)?.classList.add("sel");
  nowId = null;
  for (const b of $("filter").children) b.classList.toggle("on", b.dataset.device === (filter || ""));
  if (focus) {
    const ta = fieldEl(focus.id, focus.f);
    if (ta) { ta.focus(); ta.setSelectionRange(focus.pos, focus.pos); }
  }
  progress();
}

function progress() {
  const done = segs.filter((s) => s.text.trim()).length;
  $("progress").textContent = segs.length ? `${done} / ${segs.length} transcribed` : "";
}

function select(id, { reveal = true } = {}) {
  if (sel !== id) rowOf(sel)?.classList.remove("sel");
  sel = id;
  const s = segById(id), row = rowOf(id);
  if (s) {
    row?.classList.add("sel");
    row?.scrollIntoView({ block: "nearest" });
    setActive(s.device);
    if (reveal) {
      const a = s.start * pps - scroller.scrollLeft, b = s.end * pps - scroller.scrollLeft;
      if (a < 0 || b > W) scroller.scrollLeft = s.start * pps - Math.min(120, W * 0.2);
    }
  }
  draw();
}

function step(dir, field, andPlay) {
  const shown = shownSegs(), i = shown.findIndex((s) => s.id === sel);
  const next = i < 0 ? shown[dir > 0 ? 0 : shown.length - 1] : shown[i + dir];
  if (!next) {  // past the last one: leave the text box so the line is committed
    if (field) document.activeElement?.blur();
    return;
  }
  select(next.id);
  if (field) focusLine(next.id, field);
  if (andPlay) playSeg(next); else { stopAt = null; seek(next.start); }
}

// ---- readings: text -> yomigana -> romaji, drafted by the server (tools/reading.py) ----
// A reading line is only replaced while it is empty or still the untouched draft of the old text,
// so corrections made by hand survive. `force` (the ↻ button) rewrites both lines.
// One request at a time per line: if the line changes again while the server is answering, the loop
// goes round once more from what it just handled, so the chain of "still an untouched draft" never breaks.
const drafting = new Set();
async function draftReading(id, from, prev, force = false) {
  if (from !== "text" && from !== "kana") return;
  const lock = id + from;
  if (drafting.has(lock)) return;
  drafting.add(lock);
  try {
    for (;;) {
      const sent = segById(id)?.[from];
      if (sent == null || (!force && sent === prev)) break;
      const body = from === "text" ? { text: sent, prev_text: prev } : { kana: sent, prev_kana: prev };
      const r = await (await fetch("/api/annotate/reading", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) })).json();
      const s = segById(id);  // may be a new object after an undo
      if (!s) break;
      let any = false;
      const put = (key, value, was) => {
        if (value == null || s[key] === value || !(force || !s[key].trim() || s[key] === was)) return;
        if (key === "kana") setKana(s, value); else s[key] = value;
        any = true;
        const ta = fieldEl(id, key), row = rowOf(id);
        // the cursor may already be in this line (Tab from the text arrives before the draft does):
        // the draft is then what later edits are compared with
        if (ta) { ta.value = value; ta._prev = value; grow(ta); check(ta.parentElement, key, value); }
        if (key === "kana" && row) renderPitch(row, s);
      };
      if (from === "text") put("kana", r.kana, r.prev_kana);
      put("romaji", r.romaji, r.prev_romaji);
      if (force && from === "text" && r.kana == null) toast("No dictionary on this machine: type the よみがな, the romaji follows.", "err");
      if (any) markDirty();
      prev = sent;
      force = false;
    }
  } catch { /* offline: the lines stay as they are */ } finally {
    drafting.delete(lock);
  }
}

// ---- edits that change the set of segments ----
function pushUndo() {
  undo.push(JSON.stringify(segs));
  if (undo.length > 100) undo.shift();
  textUndoFor = null;
  $("undoBtn").disabled = false;
}
function doUndo() {
  const snap = undo.pop();
  $("undoBtn").disabled = !undo.length;
  if (!snap) return;
  segs = JSON.parse(snap);
  textUndoFor = null;
  if (!segById(sel)) sel = null;
  document.activeElement?.blur?.();
  changed();
}
function changed() {  // after any structural edit
  sortSegs();
  renderList();
  draw();
  markDirty();
}

const CJK = /[　-鿿＀-￯]/;
const join = (a, b, sep) => (!a ? b : !b ? a : a + sep + b);

function split(s) {
  if (!(t > s.start + MIN_SEG && t < s.end - MIN_SEG)) {
    return toast("Put the playhead inside the segment, where it should be cut, then split.", "err");
  }
  const el = document.activeElement;
  const caret = el?.dataset?.f === "text" && el.closest(".an-row")?.dataset.id === s.id ? el.selectionStart : null;
  pushUndo();
  const whole = s.text;
  const b = newSeg(s.device, +t.toFixed(3), s.end);
  s.end = b.start;
  if (caret != null) {  // the text is cut at the cursor; the readings are drafted again for both halves
    s.text = whole.slice(0, caret).trimEnd();
    b.text = whole.slice(caret).trimStart();
  }
  segs.push(b);
  changed();
  if (caret != null) {
    draftReading(s.id, "text", whole);
    draftReading(b.id, "text", "");
  }
  select(b.id);
}

function merge(s) {
  const next = segs.find((x) => x.device === s.device && x.start >= s.end - 1e-6);
  if (!next) return toast("There is no later segment of this speaker to merge with.", "err");
  pushUndo();
  s.end = next.end;
  s.text = join(s.text, next.text, CJK.test(s.text.slice(-1)) || CJK.test(next.text[0] || "") ? "" : " ");
  const marks = [...pitchOf(s), ...pitchOf(next)];
  s.kana = join(s.kana, next.kana, " ");
  s.pitch = formatPitch(morae(s.kana), marks);
  s.romaji = join(s.romaji, next.romaji, " ");
  segs = segs.filter((x) => x !== next);
  changed();
  select(s.id);
}

function remove(s) {
  pushUndo();
  segs = segs.filter((x) => x !== s);
  sel = null;
  changed();
  if (s.text.trim()) toast("Segment deleted. Undo brings it back.");
}

async function autoSegment() {
  let devices = tracks.filter((tr) => !segs.some((s) => s.device === tr.device)).map((tr) => tr.device);
  const replace = !devices.length;
  if (replace) {
    if (!confirm(`Replace all ${segs.length} segments with a new draft? What you typed goes too (Undo brings it back).`)) return;
    devices = tracks.map((tr) => tr.device);
  }
  $("draftBtn").disabled = true;
  try {
    const r = await (await fetch(api(`/draft?devices=${encodeURIComponent(devices.join(","))}`))).json();
    if (!r.segments.length) return toast("No speech was found. Drag on a waveform to mark segments by hand.", "err");
    pushUndo();
    if (replace) segs = [];
    for (const g of r.segments) segs.push(newSeg(g.device, g.start, g.end));
    changed();
    toast(`${r.segments.length} segments drafted from ${r.source === "silence" ? "the pauses in the audio" : r.source}. ` +
      "Check the edges as you go.");
  } catch {
    toast("Auto-segment failed.", "err");
  } finally {
    $("draftBtn").disabled = false;
  }
}

// ---- list events ----
list.addEventListener("mousedown", (e) => {  // row buttons must not take the cursor out of the text
  if (e.target.closest("[data-act]")) e.preventDefault();
});
list.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "draft") return autoSegment();
  const row = e.target.closest(".an-row"), s = row && segById(row.dataset.id);
  if (!s) return;
  const cell = e.target.closest(".an-mora");
  if (cell) {  // each click moves on: not set -> L -> H -> D -> L
    cell.focus();
    return setPitch(s, +cell.dataset.i, { _: "L", L: "H", H: "D", D: "L" }[cell.dataset.p]);
  }
  if (!act) { if (sel !== s.id) select(s.id); return; }
  if (act !== "del") select(s.id);
  if (act === "play") toggleSeg(s);
  else if (act === "read") { pushUndo(); draftReading(s.id, "text", "", true); }
  else if (act === "split") split(s);
  else if (act === "merge") merge(s);
  else if (act === "del") remove(s);
});
list.addEventListener("focusin", (e) => {
  const ta = e.target.closest("textarea"), row = e.target.closest(".an-row");
  if (ta) ta._prev = ta.value;  // to tell later whether the reading lines are still untouched drafts
  if (row && (ta || e.target.closest(".an-mora")) && sel !== row.dataset.id) select(row.dataset.id);
});
list.addEventListener("input", (e) => {
  const ta = e.target.closest("textarea");
  if (!ta) return;
  const row = ta.closest(".an-row"), s = segById(row.dataset.id), f = ta.dataset.f;
  if (textUndoFor !== s.id + f) { pushUndo(); textUndoFor = s.id + f; }  // one undo step per visit to a line
  if (f === "kana") { setKana(s, ta.value); renderPitch(row, s); } else s[f] = ta.value;
  grow(ta);
  check(ta.parentElement, f, ta.value);
  if (f === "text") { progress(); draw(); }
  markDirty();
});
list.addEventListener("change", (e) => {  // leaving a line that was edited
  const ta = e.target.closest("textarea");
  if (!ta) return;
  draftReading(ta.closest(".an-row").dataset.id, ta.dataset.f, ta._prev ?? "");
  ta._prev = ta.value;
});
list.addEventListener("keydown", (e) => {
  const ta = e.target.closest("textarea"), cell = e.target.closest(".an-mora");
  if (!(ta || cell) || e.isComposing || e.keyCode === 229) return;  // Enter / Esc belong to the IME while it is converting
  const s = segById(e.target.closest(".an-row").dataset.id);
  if (cell && !e.metaKey && !e.ctrlKey && !e.altKey) {  // pitch line: type the marks one after another
    const i = +cell.dataset.i, k = e.key.length === 1 ? e.key.toUpperCase() : e.key;
    const box = cell.closest(".an-pitch");  // the boxes are rebuilt on every change; the line itself stays
    const go = (j) => box.querySelector(`[data-i="${j}"]`)?.focus();
    const mark = { L: "L", H: "H", D: "D", ArrowUp: "H", ArrowDown: "L", Backspace: "_", Delete: "_" }[k];
    if (mark) {
      e.preventDefault();
      setPitch(s, i, mark);
      if (k.length === 1) go(i + 1); else if (k === "Backspace") go(i - 1);
      return;
    }
    if (k === "ArrowLeft" || k === "ArrowRight") { e.preventDefault(); return go(i + (k === "ArrowLeft" ? -1 : 1)); }
  }
  if (e.key === "Enter" && !e.altKey && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    step(e.shiftKey ? -1 : 1, ta ? ta.dataset.f : "pitch", true);
  } else if (e.key === "Escape") {
    e.preventDefault();
    if (e.shiftKey) playSeg(s); else toggleSeg(s);
  }
});

// ---------------- saving ----------------
let dirty = false, saving = false, conflict = false, saveTimer = 0;
function saveState(text, bad = false) {
  $("saveState").textContent = text;
  $("saveState").classList.toggle("bad", bad);
}
function markDirty() {
  dirty = true;
  if (conflict) return;
  saveState("Saving…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 700);
}
async function save() {
  if (saving || conflict || !dirty) return;
  clearTimeout(saveTimer);
  saving = true;
  dirty = false;
  let failed = false;
  try {
    const r = await fetch(api("/transcript"), { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ rev, segments: segs }) });
    if (r.status === 409) {
      conflict = dirty = true;
      banner("This transcript was changed in another window. Reload this page to continue; what you changed here " +
        "since the last save is not saved.");
      saveState("Not saved", true);
    } else if (!r.ok) {
      throw new Error(String(r.status));
    } else {
      rev = (await r.json()).rev;
      if (!dirty) saveState("Saved");
    }
  } catch {
    failed = dirty = true;
    saveState("Not saved, retrying…", true);
  }
  saving = false;
  if (dirty && !conflict) saveTimer = setTimeout(save, failed ? 3000 : 300);
}
addEventListener("beforeunload", (e) => { if (dirty || saving) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("visibilitychange", () => { if (document.hidden) save(); });

// ---------------- toolbar + keyboard ----------------
$("play").onclick = toggle;
$("speed").onchange = () => { audio.defaultPlaybackRate = audio.playbackRate = +$("speed").value; };
$("listen").onclick = (e) => {
  const b = e.target.closest("[data-listen]");
  if (!b) return;
  listen = b.dataset.listen;
  for (const x of $("listen").children) x.classList.toggle("on", x === b);
  setSource();
};
$("zoomIn").onclick = () => setZoom(pps * 1.5);
$("zoomOut").onclick = () => setZoom(pps / 1.5);
$("zoomFit").onclick = () => setZoom(0);
$("undoBtn").onclick = doUndo;
$("draftBtn").onclick = autoSegment;
$("keysBtn").onclick = () => $("keys").showModal();

let three = true;
try { three = localStorage.getItem("an.scripts3") !== "0"; } catch { /* private window */ }
$("scripts3").checked = three;
list.classList.toggle("one", !three);
$("scripts3").onchange = () => {
  list.classList.toggle("one", !$("scripts3").checked);
  for (const ta of list.querySelectorAll("textarea")) grow(ta);
  try { localStorage.setItem("an.scripts3", $("scripts3").checked ? "1" : "0"); } catch { /* ignore */ }
};

// A button clicked with the mouse should not keep the focus, or Space would press it again.
// (The pitch boxes keep it: after a click you carry on with the keyboard.)
document.addEventListener("click", (e) => { if (e.detail > 0) e.target.closest?.("button:not(.an-mora)")?.blur(); });

document.addEventListener("keydown", (e) => {
  if (!info || e.target.closest("textarea, input, select, button, a, dialog")) return;
  const s = segById(sel), mod = e.metaKey || e.ctrlKey;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (mod && k === "z") doUndo();
  else if (mod || e.altKey) return;
  else if (k === " ") toggle();
  else if (k === "ArrowLeft" || k === "ArrowRight") { stopAt = null; seek(t + (k === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 0.5 : 2)); }
  else if (k === "ArrowUp" || k === "ArrowDown") step(k === "ArrowUp" ? -1 : 1, null, false);
  else if (k === "Enter" && s) fieldEl(s.id, "text")?.focus();
  else if (k === "s" && s) split(s);
  else if (k === "m" && s) merge(s);
  else if ((k === "Delete" || k === "Backspace") && s) remove(s);
  else if (k === "+" || k === "=") setZoom(pps * 1.5);
  else if (k === "-") setZoom(pps / 1.5);
  else if (k === "?") $("keys").showModal();
  else return;
  e.preventDefault();
});

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readTheme(); draw(); });

if (sid) openSession(); else showPicker();
