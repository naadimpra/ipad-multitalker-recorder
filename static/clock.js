// NTP-style clock offset estimate against the server (keeps the lowest-RTT sample).
export class Clock {
  constructor() {
    this.samples = [];
    this.offset = null; // serverMs - performance.now()
    this.rtt = null;
  }
  ping(ws) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping", t0: performance.now() }));
  }
  onPong(m) {
    const t1 = performance.now();
    const rtt = t1 - m.t0;
    this.samples.push({ rtt, offset: m.ts - (m.t0 + t1) / 2 });
    if (this.samples.length > 30) this.samples.shift();
    const best = this.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a));
    this.offset = best.offset;
    this.rtt = best.rtt;
  }
  serverNow() {
    return this.offset == null ? null : performance.now() + this.offset;
  }
}
