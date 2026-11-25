// Collects raw mic samples (all channels, no processing) and posts interleaved
// Float32 blocks to the main thread.
//
// AI mode (the "ai" iPad in the API scenario): the worklet also PLAYS the AI's voice through its output
// and records, as one extra channel, exactly the sample it played at that moment. The AI track is then
// sample-locked to this iPad's mic, so tools.align places it on the shared timeline.
const BLOCK = 2048;
const PREBUFFER_S = 0.12;   // wait for this much AI audio before a turn starts playing (absorbs Wi-Fi jitter)
const ACTIVE_TAIL_S = 0.4;  // "AI is speaking" stays true this long after the last played sample

class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = null;
    this.ch = 0;
    this.pos = 0;
    this.frame = 0;          // frames processed since this worklet started
    this.blockStart = 0;
    this.ai = false;
    this.q = [];             // [{ turn, data }] AI audio waiting to be played
    this.qpos = 0;
    this.qFrames = 0;
    this.waitSince = -1;
    this.cur = null;         // turn currently playing
    this.lastPlay = -1e12;
    this.marks = [];         // [{ turn, kind: "start"|"end", frame }]
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === "ai") { this.clear(); this.ai = !!m.on; }
      else if (m.type === "play") { this.q.push({ turn: m.turn, data: m.data }); this.qFrames += m.data.length; }
      else if (m.type === "clear") this.clear();
    };
  }

  clear() {
    this.q = []; this.qpos = 0; this.qFrames = 0; this.waitSince = -1;
    if (this.cur !== null) { this.marks.push({ turn: this.cur, kind: "end", frame: this.frame }); this.cur = null; }
  }

  nextSample() {
    if (this.cur === null) {
      if (!this.qFrames) return 0;
      if (this.waitSince < 0) this.waitSince = this.frame;
      if (this.qFrames < PREBUFFER_S * sampleRate && this.frame - this.waitSince < PREBUFFER_S * sampleRate) return 0;
    }
    while (this.q.length) {
      const seg = this.q[0];
      if (this.qpos < seg.data.length) {
        if (this.cur !== seg.turn) {
          if (this.cur !== null) this.marks.push({ turn: this.cur, kind: "end", frame: this.frame });
          this.cur = seg.turn;
          this.marks.push({ turn: seg.turn, kind: "start", frame: this.frame });
        }
        this.lastPlay = this.frame;
        this.qFrames--;
        return seg.data[this.qpos++];
      }
      this.q.shift();
      this.qpos = 0;
    }
    // ran dry: the turn is over (or the network is late; a late chunk starts a new piece of the same turn)
    if (this.cur !== null) { this.marks.push({ turn: this.cur, kind: "end", frame: this.frame }); this.cur = null; }
    this.waitSince = -1;
    return 0;
  }

  process(inputs, outputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ich = input.length;
    const ch = ich + (this.ai ? 1 : 0);
    if (ch !== this.ch) {
      this.ch = ch;
      this.buf = new Float32Array(BLOCK * ch);
      this.pos = 0;
    }
    const out = outputs[0] && outputs[0][0];
    const n = input[0].length;
    for (let i = 0; i < n; i++) {
      if (this.pos === 0) this.blockStart = this.frame;
      for (let c = 0; c < ich; c++) this.buf[this.pos * ch + c] = input[c][i];
      if (this.ai) {
        const s = this.nextSample();
        this.buf[this.pos * ch + ich] = s;
        if (out) out[i] = s;
      }
      this.frame++;
      if (++this.pos === BLOCK) {
        this.port.postMessage({ ch, data: this.buf, frame0: this.blockStart, marks: this.marks,
                                aiActive: this.frame - this.lastPlay < ACTIVE_TAIL_S * sampleRate },
                              [this.buf.buffer]);
        this.buf = new Float32Array(BLOCK * ch);
        this.pos = 0;
        this.marks = [];
      }
    }
    return true;
  }
}

registerProcessor("capture", Capture);
