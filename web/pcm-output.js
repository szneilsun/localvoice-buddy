class PcmOutput extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.buffered = 0;
    this.preRoll = Math.ceil(0.12 * sampleRate);
    this.readyAt = null;
    this.started = false;
    this.finished = false;
    this.starved = false;
    this.port.onmessage = ({data}) => {
      if (data === "end") {
        this.finished = true;
      } else {
        const samples = new DataView(data);
        this.buffered += samples.byteLength / 2;
        this.queue.push(samples);
        this.readyAt ??= currentFrame + this.preRoll;
      }
    };
  }

  process(_inputs, outputs) {
    const channel = outputs[0][0];
    if ((!this.started && (this.readyAt === null || currentFrame < this.readyAt
                           || (!this.finished && this.buffered < this.preRoll)))
        || (this.starved && !this.finished && this.buffered < this.preRoll)) {
      if (!this.finished || this.buffered) return true;
    }
    let written = 0;
    while (written < channel.length && this.queue.length) {
      const samples = this.queue[0];
      const count = Math.min(channel.length - written, (samples.byteLength - this.offset) / 2);
      for (let i = 0; i < count; i++, this.offset += 2) {
        channel[written++] = samples.getInt16(this.offset, true) / 32768;
      }
      this.buffered -= count;
      if (this.offset === samples.byteLength) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    if (written && !this.started) {
      this.started = true;
      this.port.postMessage("started");
    }
    if (this.finished && !this.buffered) {
      this.port.postMessage("ended");
      return false;
    }
    if (this.started && written < channel.length && !this.starved) this.port.postMessage("underrun");
    this.starved = this.started && written < channel.length;
    return true;
  }
}

registerProcessor("voice-pcm-output", PcmOutput);
