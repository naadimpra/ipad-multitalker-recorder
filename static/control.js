import { Clock } from "./clock.js";
import { chirpWavUrl } from "./chirp.js";
import { EMOTIONS } from "./emotions.js";
const BUILD = "__BUILD__"; // replaced by the server

const $ = (id) => document.getElementById(id);
const POS = [["spk1", "S1"], ["spk2", "S2"], ["spk3", "S3"], ["spk4", "S4"], ["center", "Center"], ["lav", "Lav"]];
const EXPECTED = { A: ["lav"], B: ["spk1", "spk2", "spk3", "spk4", "center"] };
const PRE_CHIRP_S = 1.5; // start chirp plays this long after the session starts

const clock = new Clock();
const audio = new Audio();
const urls = {
  start: chirpWavUrl(PRE_CHIRP_S, 0.3),
  stop: chirpWavUrl(0.3, 1.0),
  extra: chirpWavUrl(0.1, 0.1),
};
let ws = null;
let state = null;
let mode = "B";
let stoppingSince = null;
let stopBusy = false;

// ---------------- connection ----------------
function connect() {
  ws = new WebSocket(`wss://${location.host}/ws`);
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "hello", role: "admin" }));
    for (let i = 0; i < 5; i++) setTimeout(() => clock.ping(ws), i * 150);
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "pong") clock.onPong(m);
    else if (m.type === "state") { state = m; render(); }
    else if (m.type === "error") show(m.text, "err", 10000);
    else if (m.type === "info") show(m.text, "info", 6000);
  };
  ws.onclose = () => { $("conn").className = "dot bad"; setTimeout(connect, 1000); };
  ws.onerror = () => ws.close();
}
const send = (m) => ws && ws.readyState === 1 && ws.send(JSON.stringify(m));

function show(text, kind, ms = 3000) {
  const d = document.createElement("div");
  d.className = `msg ${kind}`;
  d.textContent = text;
  $("msgs").prepend(d);
  setTimeout(() => d.remove(), ms);
}

// ---------------- chirp playback ----------------
// iOS only lets an <audio> element start from a user gesture. The slide gesture ends in pointerup,
// which not every iOS version counts, so the first touch anywhere "unlocks" the element with silence.
function silentWavUrl() {
  const sr = 8000, n = 800, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, n * 2, true);
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
}
const silentUrl = silentWavUrl();
let audioUnlocked = false;
function unlockAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;
  audio.src = silentUrl;
  audio.play().then(() => { if (audio.src === silentUrl) audio.pause(); }).catch(() => { audioUnlocked = false; });
}
for (const t of ["pointerdown", "touchend", "click", "keydown"]) addEventListener(t, unlockAudio, { capture: true });

// Plays a chirp through an <audio> element (works with the silent switch on).
function playChirp(kind, onChirp, onEnded) {
  try { if (navigator.audioSession) navigator.audioSession.type = "playback"; } catch {}
  const pre = kind === "start" ? PRE_CHIRP_S : kind === "stop" ? 0.3 : 0.1;
  audio.src = urls[kind];
  audio.onplaying = () => setTimeout(onChirp, pre * 1000);
  // Give the audio session back when the chirp is over, so a microphone on this device isn't blocked.
  const release = () => { try { if (navigator.audioSession) navigator.audioSession.type = "auto"; } catch {} };
  audio.onended = () => { release(); if (onEnded) onEnded(); };
  return audio.play().catch((err) => {
    release();
    show(`Chirp did not play (${err.message}). Tap Chirp to play it again.`, "err", 10000);
    if (onEnded) setTimeout(onEnded, 500);
  });
}

// ---------------- slide-to-confirm ----------------
function makeSlide(el, onDone) {
  const thumb = el.querySelector(".thumb"), label = el.querySelector(".label");
  let x0 = 0, dx = 0, max = 0, dragging = false;
  const set = (v) => {
    dx = Math.max(0, Math.min(max, v));
    thumb.style.transform = `translateX(${dx}px)`;
    label.style.opacity = max ? String(Math.max(0, 1 - (dx / max) * 1.3)) : "1";
  };
  const home = () => { thumb.style.transition = "transform .2s"; set(0); };
  thumb.addEventListener("pointerdown", (e) => {
    dragging = true; max = el.clientWidth - thumb.clientWidth - 10; x0 = e.clientX - dx;
    thumb.setPointerCapture(e.pointerId); thumb.style.transition = "none";
  });
  thumb.addEventListener("pointermove", (e) => { if (dragging) set(e.clientX - x0); });
  thumb.addEventListener("pointerup", () => {
    if (!dragging) return;
    dragging = false;
    if (dx >= max * 0.85) { thumb.style.transition = "transform .1s"; set(max); onDone(); setTimeout(home, 500); } else home();
  });
  thumb.addEventListener("pointercancel", () => { dragging = false; home(); });
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onDone(); } });
}

function preflight() {
  const devs = (state?.devices || []).filter((d) => d.online);
  const issues = [];
  for (const l of EXPECTED[mode]) {
    const d = devs.find((x) => x.label === l);
    if (!d) issues.push(`${l} offline`);
    else if (!d.armed) issues.push(`${l} not armed`);
    else if (d.ctx !== "running") issues.push(`${l} audio ${d.ctx}`);
    else if (d.quiet) issues.push(`${l} quiet`);
  }
  const labels = devs.filter((d) => d.armed).map((d) => d.label);
  for (const l of new Set(labels)) if (labels.filter((x) => x === l).length > 1) issues.push(`duplicate ${l}`);
  return issues;
}

function startSession() {
  const issues = preflight();
  if (issues.length && !confirm(`Problems:\n• ${issues.join("\n• ")}\n\nStart anyway?`)) return;
  playChirp("start", () => send({ type: "chirp", label: "start", server_ms: clock.serverNow() }));
  send({ type: "start", mode, name: $("name").value, notes: $("notes").value });
}

function stopSession() {
  if (stopBusy) return;
  stopBusy = true;
  $("slideStop").querySelector(".label").textContent = "Stopping…";
  playChirp("stop", () => send({ type: "chirp", label: "end", server_ms: clock.serverNow() }),
    () => { send({ type: "stop" }); stopBusy = false; });
}

makeSlide($("slideStart"), startSession);
makeSlide($("slideStop"), stopSession);

$("chirpBtn").onclick = () => {
  playChirp("extra", () => send({ type: "chirp", label: "extra", server_ms: clock.serverNow() }));
  show("♪ Chirp", "info", 1500);
};

// Marker: tap = instant marker, hold = marker with a label.
function addMarker(label) {
  send({ type: "marker", label, server_ms: clock.serverNow() });
  show(label ? `⚑ ${label}` : "⚑ Marker added", "info", 1800);
}
{
  const b = $("markBtn");
  let timer = null, long = false;
  b.addEventListener("pointerdown", () => {
    long = false;
    timer = setTimeout(() => { long = true; const label = prompt("Marker label (e.g. topic change, door noise):", ""); if (label !== null) addMarker(label); }, 600);
  });
  for (const t of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(t, () => clearTimeout(timer));
  b.addEventListener("click", () => { if (!long) addMarker(""); });
}

$("force").onclick = () => {
  if (confirm("Save now? Devices that haven't finished uploading will be cut short.")) send({ type: "force_close" });
};

// setup: mode + notes
for (const b of $("mode").querySelectorAll("button")) b.onclick = () => { mode = b.dataset.mode; render(); };
$("notesBtn").onclick = () => { const t = $("notes"); t.hidden = !t.hidden; if (!t.hidden) t.focus(); };

// all speaker iPads
for (const b of $("langAll").querySelectorAll("button")) b.onclick = () => send({ type: "lang_all", lang: b.dataset.lang });
$("pvAll").onchange = (e) => send({ type: "view_all", participant: e.target.checked });
$("disarmAll").onclick = () => send({ type: "disarm_all" });

// ---------------- devices ----------------
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const rows = new Map();
const open = new Set();
const emoji = (id) => (EMOTIONS.find((x) => x.id === id) || {}).emoji || "";
const posRank = (l) => { const i = POS.findIndex(([v]) => v === l); return i < 0 ? 99 : i; };

function makeRow(d) {
  const wrap = document.createElement("div");
  wrap.className = "devwrap";
  wrap.innerHTML = `
    <div class="dev">
      <div class="devhead">
        <span class="dot"></span>
        <div class="devname"><b class="title"></b><small class="who"></small></div>
        <div class="devmeter"><div class="meter"><div></div></div></div>
        <div class="devflags"></div>
        <span class="chev">›</span>
      </div>
      <div class="devpanel" hidden>
        <div class="chips pos">${POS.map(([v, t]) => `<button class="chip" data-pos="${v}">${t}</button>`).join("")}</div>
        <input class="spk" placeholder="Participant ID" autocomplete="off" autocapitalize="off">
        <div class="row">
          <div class="seg lg"><button data-lang="ja">日本語</button><button data-lang="en">English</button></div>
          <span class="grow"></span><span class="muted">Participant view</span>
          <span class="switch"><input type="checkbox" class="pv"><i></i></span>
        </div>
        <div class="row"><button class="ghost small ident">Identify</button><button class="ghost small disarm" hidden>Disarm</button><button class="danger small rm" hidden>Remove</button>
          <span class="muted grow info" style="text-align:right"></span></div>
      </div>
    </div>`;
  const dev = wrap.querySelector(".dev"), head = wrap.querySelector(".devhead"), panel = wrap.querySelector(".devpanel");
  let justSwiped = false;
  head.onclick = () => {
    if (justSwiped) return;
    const o = !open.has(d.id);
    if (o) open.add(d.id); else open.delete(d.id);
    panel.hidden = !o; dev.classList.toggle("open", o);
  };
  for (const b of wrap.querySelectorAll(".pos .chip")) b.onclick = () => send({ type: "assign", id: d.id, label: b.dataset.pos });
  wrap.querySelector(".spk").onchange = (e) => send({ type: "assign", id: d.id, speaker: e.target.value });
  for (const b of wrap.querySelectorAll(".lg button")) b.onclick = () => send({ type: "lang", id: d.id, lang: b.dataset.lang });
  wrap.querySelector(".pv").onchange = (e) => send({ type: "view", id: d.id, participant: e.target.checked });
  wrap.querySelector(".ident").onclick = () => send({ type: "identify", id: d.id });
  wrap.querySelector(".disarm").onclick = () => send({ type: "disarm", id: d.id });
  wrap.querySelector(".rm").onclick = () => send({ type: "remove", id: d.id });

  // swipe left to remove an offline device
  let sx = null, dx = 0, swiping = false;
  head.addEventListener("pointerdown", (e) => {
    if (!wrap.dataset.offline) return;
    sx = e.clientX; dx = 0; swiping = false;
  });
  dev.addEventListener("pointermove", (e) => {
    if (sx == null) return;
    dx = e.clientX - sx;
    if (!swiping && Math.abs(dx) > 8) { swiping = true; dev.setPointerCapture(e.pointerId); wrap.classList.add("swiping"); dev.style.transition = "none"; }
    if (swiping) dev.style.transform = `translateX(${Math.min(0, dx)}px)`;
  });
  const endSwipe = () => {
    if (sx == null) return;
    const remove = swiping && dx < -90;
    if (swiping) { justSwiped = true; setTimeout(() => (justSwiped = false), 80); }
    sx = null; dev.style.transition = "transform .18s";
    if (remove) { dev.style.transform = "translateX(-110%)"; setTimeout(() => send({ type: "remove", id: d.id }), 150); }
    else { dev.style.transform = ""; wrap.classList.remove("swiping"); }
  };
  dev.addEventListener("pointerup", endSwipe);
  dev.addEventListener("pointercancel", endSwipe);

  rows.set(d.id, wrap);
  return wrap;
}

function flagsFor(d, s) {
  const f = [];
  if (!d.online) f.push(["offline", "bad"]);
  else {
    if (!d.armed) f.push(["not armed", "warn"]);
    if (d.hidden) f.push(["background", "bad"]);
    if (d.armed && d.ctx !== "running") f.push([`audio ${d.ctx}`, "bad"]);
    if (d.armed && d.quiet) f.push(["quiet", "warn"]);
    if (state.build && d.build !== state.build) f.push(["old page", "bad"]);
    if (d.problem) f.push(["error", "bad"]);
    if (s && d.armed && !s.members.includes(d.label)) f.push(["not in session", "warn"]);
    if (d.backlog_s > 1.5) f.push([`upload ${d.backlog_s.toFixed(0)}s`, ""]);
  }
  return f;
}

function updateRow(wrap, d, s) {
  const flags = flagsFor(d, s);
  const st = flags.some(([, c]) => c === "bad") ? "bad" : flags.some(([, c]) => c === "warn") ? "warn" : d.recording ? "rec" : "ok";
  wrap.dataset.offline = d.online ? "" : "1";
  wrap.querySelector(".dot").className = `dot ${st}`;
  wrap.querySelector(".title").textContent = d.label || "?";
  wrap.querySelector(".who").textContent = d.speaker || "";
  const db = d.level_db ?? -120, bar = wrap.querySelector(".meter > div");
  bar.style.width = d.armed && d.online ? `${Math.max(0, Math.min(100, (db + 60) * (100 / 60)))}%` : "0%";
  bar.classList.toggle("hot", (d.peak_db ?? -120) > -1);
  const fl = wrap.querySelector(".devflags");
  fl.replaceChildren(...flags.map(([t, c]) => Object.assign(document.createElement("span"), { className: `flag ${c}`, textContent: t })));
  if (d.recording && d.emotion) fl.prepend(Object.assign(document.createElement("span"), { className: "emoji", textContent: emoji(d.emotion) }));

  const o = open.has(d.id);
  wrap.querySelector(".devpanel").hidden = !o;
  wrap.querySelector(".dev").classList.toggle("open", o);
  for (const b of wrap.querySelectorAll(".pos .chip")) { b.classList.toggle("on", b.dataset.pos === d.label); b.disabled = !!d.recording; }
  const spk = wrap.querySelector(".spk");
  if (document.activeElement !== spk) spk.value = d.speaker || "";
  spk.disabled = !!d.recording;
  for (const b of wrap.querySelectorAll(".lg button")) { b.classList.toggle("on", b.dataset.lang === (d.lang === "en" ? "en" : "ja")); b.disabled = !d.online; }
  const pv = wrap.querySelector(".pv");
  pv.checked = !!d.participant; pv.disabled = !d.online;
  wrap.querySelector(".ident").hidden = !d.online;
  const da = wrap.querySelector(".disarm");
  da.hidden = !(d.online && d.armed);
  da.disabled = !!d.recording || !!s;  // never while a session is running
  wrap.querySelector(".rm").hidden = d.online;
  wrap.querySelector(".info").textContent = [
    d.sample_rate ? `${(d.sample_rate / 1000).toFixed(1)} kHz · ${d.channels} ch` : null,
    d.rtt_ms != null ? `${d.rtt_ms.toFixed(0)} ms` : null,
    d.problem || null,
  ].filter(Boolean).join(" · ");
}

function staleWarnings() {
  const out = [];
  // An old server can't stamp pages, so every page would look "old". Say what is actually wrong.
  if (!state.build) return ["The server is running old code — stop it (Ctrl+C in its terminal) and run python server.py again."];
  if (state.server_stale) out.push("server.py changed since the server started — stop it (Ctrl+C) and run python server.py again.");
  if (BUILD !== state.build) out.push("This admin page is out of date — reload it.");
  const old = state.devices.filter((d) => d.online && d.build !== state.build).map((d) => d.label);
  if (old.length) out.push(`Out-of-date recorder page on: ${old.join(", ")} — reload that iPad's page (close the tab if it persists).`);
  return out;
}

// ---------------- render ----------------
function render() {
  const warn = staleWarnings();
  $("stale").hidden = !warn.length;
  $("stale").textContent = warn.join("  ");
  $("conn").className = `dot ${ws.readyState === 1 ? "ok" : "bad"}`;

  const s = state.session;
  const recording = s?.state === "recording";
  const stopping = s?.state === "stopping";
  const devs = state.devices;
  const online = devs.filter((d) => d.online);

  // hero
  const hero = $("heroState"), sub = $("heroSub");
  const issues = s ? [] : preflight();
  if (recording) {
    const now = clock.serverNow() ?? state.server_ms;
    hero.textContent = `● ${fmt((now - s.started_at) / 1000)}`; hero.className = "rec";
    sub.textContent = `${s.members.length} ${s.members.length === 1 ? "device" : "devices"}`;
  } else if (stopping) {
    hero.textContent = "Saving…"; hero.className = "";
    sub.textContent = `${s.finished.length} of ${s.members.length} uploaded`;
  } else if (!online.length) {
    hero.textContent = "No devices"; hero.className = "";
    sub.textContent = state.last_session ? `last: ${state.last_session}` : "";
  } else if (issues.length) {
    hero.textContent = `${issues.length} to fix`; hero.className = "warn";
    sub.textContent = issues.slice(0, 3).join(" · ") + (issues.length > 3 ? ` +${issues.length - 3}` : "");
  } else {
    hero.textContent = "Ready"; hero.className = "ok";
    sub.textContent = state.last_session ? `last: ${state.last_session}` : "";
  }
  $("hero").classList.toggle("rec", recording);
  $("sessName").textContent = s ? (s.name || s.id) : "";

  $("setup").hidden = !!s;
  for (const b of $("mode").querySelectorAll("button")) b.classList.toggle("on", b.dataset.mode === mode);
  $("slideStart").hidden = !!s;
  $("slideStop").hidden = !recording;
  if (!stopBusy) $("slideStop").querySelector(".label").textContent = "Slide to stop";
  $("live").hidden = !recording;
  if (stopping) stoppingSince ??= Date.now(); else stoppingSince = null;
  $("force").hidden = !(stoppingSince && Date.now() - stoppingSince > 15000);

  // all speaker iPads
  const spk = online.filter((d) => String(d.label).startsWith("spk"));
  $("group").hidden = !spk.length;
  if (spk.length) {
    $("pvAll").checked = spk.every((d) => d.participant);
    const armedN = spk.filter((d) => d.armed).length;
    $("armCount").textContent = `${armedN} of ${spk.length} armed`;
    $("disarmAll").hidden = armedN === 0;
    $("disarmAll").disabled = !!s;
    const langs = new Set(spk.map((d) => (d.lang === "en" ? "en" : "ja")));
    for (const b of $("langAll").querySelectorAll("button")) b.classList.toggle("on", langs.size === 1 && langs.has(b.dataset.lang));
  }

  // devices
  $("empty").hidden = devs.length > 0;
  const sorted = [...devs].sort((a, b) => posRank(a.label) - posRank(b.label) || String(a.label).localeCompare(String(b.label)));
  const seen = new Set();
  sorted.forEach((d, i) => {
    seen.add(d.id);
    const wrap = rows.get(d.id) || makeRow(d);
    updateRow(wrap, d, s);
    const host = $("devices");
    if (host.children[i] !== wrap) host.insertBefore(wrap, host.children[i] || null);
  });
  for (const [id, wrap] of rows) if (!seen.has(id)) { wrap.remove(); rows.delete(id); open.delete(id); }
}

setInterval(() => clock.ping(ws), 2000);
connect();
