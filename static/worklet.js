// Collects raw mic samples (all channels, no processing) and posts interleaved
// Float32 blocks to the main thread.
const BLOCK = 2048;

class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = null;
    this.ch = 0;
    this.pos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    if (input.length !== this.ch) {
      this.ch = input.length;
      this.buf = new Float32Array(BLOCK * this.ch);
      this.pos = 0;
    }
    const n = input[0].length;
    for (let i = 0; i < n; i++) {
      for (let c = 0; c < this.ch; c++) this.buf[this.pos * this.ch + c] = input[c][i];
      if (++this.pos === BLOCK) {
        this.port.postMessage({ ch: this.ch, data: this.buf }, [this.buf.buffer]);
        this.buf = new Float32Array(BLOCK * this.ch);
        this.pos = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture", Capture);
