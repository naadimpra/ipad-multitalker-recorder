import { Clock } from "./clock.js";
const BUILD = "__BUILD__"; // replaced by the server; shown to the admin so stale pages are noticed
import { EMOTIONS } from "./emotions.js";

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

const rand = () => (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36));
const baseId = store.get("deviceId") || rand();
store.set("deviceId", baseId);
// Each tab gets its own id, so several recorder tabs in one browser don't overwrite each other.
let tabId;
try { tabId = sessionStorage.getItem("tabId") || rand().slice(0, 8); sessionStorage.setItem("tabId", tabId); }
catch { tabId = rand().slice(0, 8); }
const deviceId = `${baseId}-${tabId}`;
$("label").value = store.get("label") || "spk1";
const POS = [["spk1", "S1"], ["spk2", "S2"], ["spk3", "S3"], ["spk4", "S4"], ["center", "Center"], ["lav", "Lav"]];
function renderChips() { for (const b of $("posChips").children) b.classList.toggle("on", b.dataset.pos === $("label").value); }
for (const [v, t] of POS) {
  const b = document.createElement("button");
  b.className = "chip"; b.dataset.pos = v; b.textContent = t;
  b.onclick = () => { $("label").value = v; store.set("label", v); renderChips(); };
  $("posChips").append(b);
}
renderChips();
$("speaker").value = store.get("speaker") || "";

const clock = new Clock();
let ws = null;
let ctx = null, stream = null, node = null, wakeLock = null;
let armed = false, channels = 0, sampleRate = 0;
let problem = "";
let micLabel = "";

// Current recording. Chunks stay in `queue` until the server acks them, so a
// Wi-Fi drop only delays upload; nothing is lost while this page stays open.
let rec = null;
let seqCounter = 0;
const queue = new Map(); // seq -> { buf, sent }
let level = { rms: 0, peak: 0 };
let interruptedAt = null;
const QUIET_DB = -45, QUIET_WINDOW = 80; // 80 x 250 ms = 20 s
const recentDb = [];
let quiet = false;
let emotion = "neutral"; // current emotion; logged with a frame position while recording

// ---------------- networking ----------------
function connect() {
  ws = new WebSocket(`wss://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "hello", role: "device", id: deviceId, ...describe() }));
    for (const q of queue.values()) q.sent = false;
    for (let i = 0; i < 5; i++) setTimeout(() => clock.ping(ws), i * 150);
    pump();
    checkFinished();
  };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1000);
  ws.onerror = () => ws.close();
}

function handle(m) {
  switch (m.type) {
    case "pong": clock.onPong(m); break;
    case "ack": queue.delete(m.seq); checkFinished(); break;
    case "start": startRec(m.sid); break;
    case "stop": if (rec && rec.sid === m.sid) stopRec(); break;
    case "resume":
      if (rec && rec.sid === m.sid) {
        if (m.state === "stopping" && !rec.stopped) stopRec();
      } else {
        problem = "This page was reloaded during a session — its recording was lost.";
      }
      break;
    case "assign":
      if (rec && !rec.stopped) break;
      if (m.label) { $("label").value = m.label; store.set("label", m.label); renderChips(); }
      if (m.speaker != null) { $("speaker").value = m.speaker; store.set("speaker", m.speaker); }
      break;
    case "view": setParticipant(!!m.participant); break;
    case "disarm": disarm(); break;
    case "lang": setLang(m.lang); break;
    case "identify":
      document.body.classList.remove("flash"); void document.body.offsetWidth;
      document.body.classList.add("flash");
      break;
  }
}

function describe() {
  return {
    label: $("label").value, speaker: $("speaker").value, armed,
    sample_rate: sampleRate, channels, mic: micLabel, ua: navigator.userAgent,
    participant: document.body.classList.contains("participant"), lang, build: BUILD,
  };
}

function pump() {
  if (!ws || ws.readyState !== 1) return;
  for (const q of queue.values()) if (!q.sent) { ws.send(q.buf); q.sent = true; }
}

// ---------------- recording ----------------
function startRec(sid) {
  if (!armed || !ctx || ctx.state !== "running") {
    problem = "Start received but microphone not armed/running!";
    return;
  }
  problem = "";
  rec = { sid, framesSent: 0, pend: [], pendFrames: 0, stopped: false, finishedSent: false,
          t0: null, events: [], emotions: [], startedPerf: performance.now() };
  logEmotion(emotion); // every recording starts with an emotion label at frame 0
}

function stopRec() {
  rec.stopped = true;
  flush();
  checkFinished();
}

// ---------------- emotion labels ----------------
// A label applies from its frame until the next one. `frame` counts audio frames of this
// device's own recording, so tools.align can place it on the aligned timeline.
function logEmotion(id) {
  const ev = { frame: rec.framesSent + rec.pendFrames, server_ms: clock.serverNow(), emotion: id };
  rec.emotions.push(ev);
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "emotion", sid: rec.sid, ...ev }));
}

function setEmotion(id) {
  if (id === emotion) return;
  emotion = id;
  if (rec && !rec.stopped) logEmotion(id);
  renderEmotions();
}

function renderEmotions() {
  for (const b of $("emoGrid").children) b.classList.toggle("on", b.dataset.id === emotion);
}

for (const e of EMOTIONS) {
  const b = document.createElement("button");
  b.className = "emo";
  b.dataset.id = e.id;
  b.style.setProperty("--c", e.color);
  const em = document.createElement("span"); em.className = "em"; em.textContent = e.emoji;
  const ja = document.createElement("span"); ja.className = "ja"; ja.textContent = e.ja;
  const en = document.createElement("span"); en.className = "en"; en.textContent = e.en;
  b.append(em, ja, en);
  b.addEventListener("pointerdown", (ev) => { ev.preventDefault(); setEmotion(e.id); });
  $("emoGrid").append(b);
}
renderEmotions();

// Participant view hides everything except the emotion buttons. The Back button (or the admin
// phone) returns to the setup view; leaving the view never stops a recording.
function setParticipant(on) {
  document.body.classList.toggle("participant", on);
}
$("toPart").onclick = () => setParticipant(true);
$("pback").onclick = () => {
  if (rec && !rec.stopped && !confirm(UI.confirmBack)) return;
  setParticipant(false);
};

// ---------------- language (which label is big) ----------------
// ja: Japanese emotion label big, English small. en: English big, Japanese small. Only the emotion
// labels and the participant hint follow the language; status text stays English for the operator.
const HINT = {
  ja: "話しながら、今の気持ちに一番近いボタンを押してください。",
  en: "While you speak, tap the button closest to how you feel right now.",
};
const UI = { confirmBack: "Go back to setup? Recording continues." };
let lang = new URLSearchParams(location.search).get("lang") || store.get("lang") || "ja";
if (!HINT[lang]) lang = "ja";
function setLang(l) {
  lang = HINT[l] ? l : "ja";
  store.set("lang", lang);
  document.body.classList.toggle("lang-en", lang === "en");
  $("phint").textContent = HINT[lang];
  for (const b of document.querySelectorAll(".plang button")) b.classList.toggle("on", b.dataset.lang === lang);
}
for (const b of document.querySelectorAll(".plang button")) b.addEventListener("click", () => setLang(b.dataset.lang));
setLang(lang);
if (new URLSearchParams(location.search).get("view") === "participant") setParticipant(true);

function checkFinished() {
  if (!rec || !rec.stopped || rec.finishedSent || queue.size > 0) return;
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "finished", sid: rec.sid, frames: rec.framesSent, events: rec.events,
                          emotions: rec.emotions }));
  rec.finishedSent = true;
}

function append(data) {
  if (rec.framesSent === 0 && rec.pendFrames === 0) {
    const now = clock.serverNow();
    rec.t0 = now == null ? null : now - ((data.length / channels) / sampleRate) * 1000 - (ctx.baseLatency || 0) * 1000;
  }
  rec.pend.push(data);
  rec.pendFrames += data.length / channels;
  if (rec.pendFrames >= sampleRate) flush();
}

function flush() {
  if (!rec || rec.pendFrames === 0) return;
  const n = rec.pendFrames;
  const pcm = new Float32Array(n * channels);
  let o = 0;
  for (const b of rec.pend) { pcm.set(b, o); o += b.length; }
  const hdr = { sid: rec.sid, seq: seqCounter, start: rec.framesSent, frames: n, ch: channels };
  if (rec.framesSent === 0) hdr.t0_server_ms = rec.t0;
  const hb = new TextEncoder().encode(JSON.stringify(hdr));
  const msg = new Uint8Array(4 + hb.length + pcm.byteLength);
  new DataView(msg.buffer).setUint32(0, hb.length, true);
  msg.set(hb, 4);
  msg.set(new Uint8Array(pcm.buffer), 4 + hb.length);
  queue.set(seqCounter++, { buf: msg.buffer, sent: false });
  rec.framesSent += n;
  rec.pend = [];
  rec.pendFrames = 0;
  pump();
}

function onBlock({ ch, data }) {
  channels = ch;
  let peak = 0, sum = 0;
  for (let i = 0; i < data.length; i++) { const a = Math.abs(data[i]); sum += a * a; if (a > peak) peak = a; }
  level.rms = Math.max(level.rms, Math.sqrt(sum / data.length));
  level.peak = Math.max(level.peak, peak);
  if (rec && !rec.stopped) append(data);
}

// Audio was interrupted (Siri, call, app switch). Fill the gap with silence
// so this track's timeline stays roughly aligned; the event is logged.
function onCtxState() {
  const st = ctx.state;
  if (st !== "running") {
    if (interruptedAt == null) interruptedAt = performance.now();
    if (rec && !rec.stopped) rec.events.push({ type: "interrupted", frame: rec.framesSent + rec.pendFrames,
                                               server_ms: clock.serverNow(), state: st });
    problem = `Audio ${st} — tap the screen to resume`;
  } else if (interruptedAt != null) {
    const gapMs = performance.now() - interruptedAt;
    interruptedAt = null;
    problem = "";
    if (rec && !rec.stopped) {
      const gap = Math.round((gapMs / 1000) * sampleRate);
      rec.events.push({ type: "resumed", frame: rec.framesSent + rec.pendFrames, gap_frames: gap,
                        server_ms: clock.serverNow() });
      for (let left = gap; left > 0; left -= 4096) append(new Float32Array(Math.min(4096, left) * channels));
    }
  }
}

async function populateMics() {
  try {
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    const sel = $("mic"), saved = store.get("mic") || "";
    sel.replaceChildren(new Option("Default microphone", ""));
    devs.filter((d) => d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications")
      .forEach((d, i) => sel.append(new Option(d.label || `Input ${i + 1}`, d.deviceId)));
    sel.value = [...sel.options].some((o) => o.value === saved) ? saved : "";
    $("micRow").hidden = sel.options.length <= 2;  // only worth showing when there is a choice
  } catch {}
}

async function openMic(wantCh) {
  const base = { echoCancellation: false, noiseSuppression: false, autoGainControl: false,
                 channelCount: { ideal: wantCh } };
  const id = $("mic").value;
  const gum = (a) => navigator.mediaDevices.getUserMedia({ audio: a });
  if (!id) return gum(base);
  try {
    return await gum({ ...base, deviceId: { exact: id } });
  } catch (e) {
    if (e.name !== "OverconstrainedError" && e.name !== "NotFoundError") throw e;
    $("mic").value = "";  // saved input is no longer attached
    return gum(base);
  }
}

// Builds ctx -> worklet -> (muted) destination for the open stream. Tries the microphone's own
// sample rate first, because some browsers refuse to connect a stream to a context running
// at a different rate; falls back to the default rate.
async function buildGraph(track, trackCh) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const micRate = track.getSettings().sampleRate;
  const attempts = micRate ? [micRate, undefined] : [undefined];
  let lastErr;
  for (const rate of attempts) {
    let c = null;
    try {
      c = rate ? new Ctx({ sampleRate: rate }) : new Ctx();
      await c.audioWorklet.addModule("/static/worklet.js");
      const n = new AudioWorkletNode(c, "capture", {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        channelCount: trackCh, channelCountMode: "explicit", channelInterpretation: "discrete",
      });
      c.createMediaStreamSource(stream).connect(n);
      const mute = c.createGain();
      mute.gain.value = 0;
      n.connect(mute).connect(c.destination);
      return { ctx: c, node: n };
    } catch (e) {
      lastErr = e;
      try { if (c) await c.close(); } catch {}
    }
  }
  throw lastErr;
}

const iosVersion = (navigator.userAgent.match(/OS (\d+)[_.](\d+)/) || [])[0] || "";

// Returns why this browser can't record, or "" if it can.
function unsupportedReason() {
  const old = "Update iPadOS: Settings → General → Software Update (needs iPadOS 14.5 or newer).";
  if (!(window.AudioContext || window.webkitAudioContext)) return `This Safari is too old to record audio. ${old}`;
  if (typeof AudioWorkletNode === "undefined") return `This Safari has no AudioWorklet. ${old}`;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
    return "Microphone unavailable: open this page via https:// with the trusted certificate installed.";
  return "";
}

let arming = false;
async function arm() {
  const unsupported = unsupportedReason();
  if (unsupported) { problem = unsupported; return; }
  arming = true;
  let step = "microphone";
  // Safari/iPadOS: capture needs the "play-and-record" audio category. Another page that left it on
  // "playback" (e.g. the Control page's chirp player) makes getUserMedia fail with InvalidStateError.
  try { if (navigator.audioSession) navigator.audioSession.type = "play-and-record"; } catch {}
  try {
    stream = await openMic($("label").value === "lav" ? 4 : 1);
    await populateMics();  // labels are only available after permission
    store.set("mic", $("mic").value);
    step = "audio engine";
    const track = stream.getAudioTracks()[0];
    const trackCh = track.getSettings().channelCount || 1;
    ({ ctx, node } = await buildGraph(track, trackCh));
    node.port.onmessage = (e) => onBlock(e.data);
    step = "start audio";
    await ctx.resume();
    ctx.onstatechange = onCtxState;
    track.onended = () => { problem = "Microphone was taken by another app!"; };
    sampleRate = ctx.sampleRate;
    channels = trackCh;
    micLabel = track.label || "";
    armed = true;
    await keepAwake();
  } catch (err) {
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    const hint = /AudioSession/i.test(err.message || "")
      ? " — close other tabs/apps that play or record audio (e.g. the Control page on this iPad), then tap Arm again." : "";
    problem = `Mic error (${step}): ${err.name || ""} ${err.message || err}${hint}`.trim();
  } finally {
    arming = false;
  }
}

// Releases the microphone and audio engine. Used by Disarm (tap or from the admin) and by re-arming.
async function releaseMic() {
  armed = false; quiet = false; recentDb.length = 0;
  try {
    if (ctx) ctx.onstatechange = null;
    if (node) node.port.onmessage = null;
    if (stream) stream.getTracks().forEach((t) => { t.onended = null; t.stop(); });
    if (ctx) await ctx.close();
    if (wakeLock) { await wakeLock.release(); wakeLock = null; }
  } catch {}
  stream = ctx = node = null; sampleRate = 0; channels = 0; micLabel = ""; interruptedAt = null;
  if (/^(Audio|Microphone)/.test(problem)) problem = "";
  try { if (navigator.audioSession) navigator.audioSession.type = "auto"; } catch {}
}

// Never while recording: that would cut the take. Uploads of a finished take are not affected.
async function disarm() {
  if (!armed || (rec && !rec.stopped) || arming) return;
  await releaseMic();
}

async function keepAwake() {
  try {
    if ("wakeLock" in navigator && !document.hidden) wakeLock = await navigator.wakeLock.request("screen");
  } catch {}
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (rec && !rec.stopped) rec.events.push({ type: "hidden", server_ms: clock.serverNow() });
  } else {
    keepAwake();
  }
});
document.addEventListener("touchend", () => { if (ctx && ctx.state !== "running") ctx.resume(); });

$("arm").onclick = () => (armed ? disarm() : arm());
// Switching the input while armed (and not recording) re-opens the microphone with the new one.
async function rearm() {
  await releaseMic();
  await arm();
}
$("mic").onchange = () => { store.set("mic", $("mic").value); if (armed && !(rec && !rec.stopped)) rearm(); };
for (const id of ["label", "speaker"]) {
  $(id).onchange = () => { store.set(id, $(id).value); };
}

// ---------------- UI + status ----------------
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

setInterval(() => {
  const recording = !!rec && !rec.stopped;
  const backlogS = [...queue.values()].reduce((a, q) => a + q.buf.byteLength, 0) / (4 * Math.max(1, channels) * (sampleRate || 48000));
  const db = level.rms > 0 ? 20 * Math.log10(level.rms) : -120;
  const pdb = level.peak > 0 ? 20 * Math.log10(level.peak) : -120;

  let status = armed ? "Ready" : "Not armed", scls = armed ? "ok" : "";
  if (recording) { status = `● REC ${fmt((performance.now() - rec.startedPerf) / 1000)}`; scls = "rec"; }
  else if (rec && !rec.finishedSent) status = `Uploading… ${backlogS.toFixed(0)} s left`;
  else if (rec) { status = "Saved ✓"; scls = "ok"; }
  if (problem) { status = `⚠ ${problem}`; scls = "bad"; }
  $("status").textContent = status;
  $("status").className = scls;
  // participant header: only show a chip when something needs attention
  const recFrames = rec ? rec.framesSent + rec.pendFrames : 0;
  $("ptime").textContent = rec ? fmt(recFrames / (sampleRate || 48000)) : "0:00";
  $("plvl").style.width = `${Math.max(0, Math.min(100, (db + 60) * (100 / 60)))}%`;
  $("plvl").classList.toggle("hot", pdb > -1);
  const uploading = !recording && rec && !rec.finishedSent;
  const [ccls, ctxt] = problem ? ["bad", "⚠ Tell staff"] : !armed ? ["warn", "Mic off"]
    : uploading ? ["up", `Uploading ${backlogS.toFixed(0)}s`] : !recording && rec ? ["saved", "✓ Saved"] : [null, ""];
  $("pchip").className = ccls ? `show ${ccls}` : "";
  $("pchip").textContent = ctxt;
  $("phint").style.visibility = recording || uploading ? "hidden" : "visible";
  document.body.classList.toggle("recording", recording);
  $("label").disabled = $("speaker").disabled = $("mic").disabled = recording;
  $("arm").disabled = recording || arming;
  $("arm").textContent = armed ? "Disarm" : arming ? "Arming…" : "Arm microphone";
  $("arm").className = armed ? "big ghost" : "big";
  for (const c of $("posChips").children) c.disabled = recording;

  if (armed) {
    recentDb.push(db);
    if (recentDb.length > QUIET_WINDOW) recentDb.shift();
    quiet = recentDb.length === QUIET_WINDOW && Math.max(...recentDb) < QUIET_DB;
  } else { recentDb.length = 0; quiet = false; }
  $("quietWarn").style.display = quiet ? "" : "none";

  $("lvl").style.width = `${Math.max(0, Math.min(100, (db + 60) * (100 / 60)))}%`;
  $("lvl").classList.toggle("hot", pdb > -1);
  $("db").textContent = armed ? `${db.toFixed(0)} dB` : "–";
  $("info").textContent = [
    armed ? `${(sampleRate / 1000).toFixed(1)} kHz · ${channels} ch` : "mic off",
    ws && ws.readyState === 1 ? "online" : "OFFLINE",
    clock.rtt != null ? `${clock.rtt.toFixed(0)} ms` : "connecting…",
    iosVersion,
    lang,
    `v${BUILD.slice(0, 6)}`,
  ].filter(Boolean).join(" · ");

  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({
      type: "status", ...describe(), ctx: ctx ? ctx.state : null, hidden: document.hidden,
      level_db: db, peak_db: pdb, rtt_ms: clock.rtt, offset_ms: clock.offset,
      backlog_s: backlogS, quiet, emotion, recording, sid: rec ? rec.sid : null, problem,
    }));
  }
  level = { rms: 0, peak: 0 };
}, 250);

setInterval(() => clock.ping(ws), 2000);
problem = unsupportedReason();
connect();
