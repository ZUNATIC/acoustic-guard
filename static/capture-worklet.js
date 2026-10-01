// Runs on the browser's audio thread. Converts whatever rate the microphone delivers
// (usually 44.1 or 48 kHz) to 16 kHz mono int16 and posts 512-sample frames (32 ms) -
// exactly the block size the server's voice activity detector works on.

const TARGET_RATE = 16000;
const FRAME = 512;

class Pcm16kCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(FRAME);
    this.fill = 0;
  }

  push(value) {
    const v = value > 1 ? 1 : value < -1 ? -1 : value;
    this.frame[this.fill++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.fill === FRAME) {
      this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
      this.frame = new Int16Array(FRAME);
      this.fill = 0;
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    if (this.step === 1) {
      for (let i = 0; i < channel.length; i++) this.push(channel[i]);
      return true;
    }
    // box-filter decimation: average the input samples that fall into each output sample
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i];
      this.count++;
      this.phase += 1;
      if (this.phase >= this.step) {
        this.phase -= this.step;
        this.push(this.sum / this.count);
        this.sum = 0;
        this.count = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm16k-capture", Pcm16kCapture);
