const assets = "/vad/";
const frameSamples = 512;
const contextSamples = 64;
const leadFrames = 32;
const startFrames = 5;
const idleEvidence = 3;
const silenceFrames = 16;
const maxFrames = Math.floor(15 * 16000 / frameSamples);
const minSamples = 4800;
const steadyQueueFrames = 12;
let modelPromise;

export function preloadVoiceInput() {
  modelPromise ??= (async () => {
    const ort = await import(`${assets}ort.wasm.min.mjs`);
    ort.env.wasm.numThreads = 1;
    ort.env.wasm.wasmPaths = assets;
    const session = await ort.InferenceSession.create(`${assets}silero_vad_v5.onnx`, {
      executionProviders: ["wasm"],
    });
    return {ort, session};
  })().catch((error) => {
    modelPromise = null;
    throw error;
  });
  return modelPromise;
}

export class VoiceSegments {
  constructor({start, end, playing = () => false}) {
    Object.assign(this, {start, end, playing});
    this.reset();
  }

  reset() {
    this.lead = [];
    this.clip = null;
    this.voiced = 0;
    this.recentSpeech = [];
    this.wasPlaying = false;
    this.quiet = 0;
    this.blocked = false;
  }

  suppress() {
    this.reset();
    this.blocked = true;
  }

  finish(trim = 0, block = true) {
    if (!this.clip) return;
    const frames = this.clip.slice(0, this.clip.length - trim);
    const samples = new Float32Array(Math.max(minSamples, frames.length * frameSamples));
    for (const [index, frame] of frames.entries()) samples.set(frame, index * frameSamples);
    if (block) this.suppress(); else this.reset();
    this.end(samples);
  }

  push(score, frame) {
    this.quiet = score < 0.35 ? this.quiet + 1 : 0;
    if (this.blocked) {
      if (this.quiet >= silenceFrames) this.reset();
      return;
    }
    if (this.clip) {
      this.clip.push(frame);
      if (this.quiet >= silenceFrames) this.finish(silenceFrames - 4, false);
      else if (this.clip.length >= maxFrames) this.finish();
      return;
    }
    this.lead.push(frame);
    if (this.lead.length > leadFrames) this.lead.shift();
    const playing = !!this.playing();
    if (playing !== this.wasPlaying) {
      this.voiced = 0;
      this.recentSpeech.length = 0;
      this.wasPlaying = playing;
    }
    // At rest, a brief low-confidence consonant should not restart onset detection.
    let confirmed;
    if (playing) {
      this.voiced = score >= 0.72 ? this.voiced + 1 : 0;
      confirmed = this.voiced >= startFrames;
    } else {
      this.recentSpeech.push(score >= 0.55);
      if (this.recentSpeech.length > startFrames) this.recentSpeech.shift();
      confirmed = this.recentSpeech.filter(Boolean).length >= idleEvidence;
    }
    if (confirmed) {
      if (!this.start()) this.suppress();
      else {
        this.clip = this.lead;
        this.lead = [];
      }
    }
  }
}

export function prepareVoiceInput(context) {
  return context.audioWorklet.addModule(`${assets}vad.worklet.bundle.min.js`);
}

export function connectVoiceInput(context, stream, handlers) {
  if (context.state === "closed") throw new Error("Microphone was closed.");
  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "vad-helper-worklet", {
    channelCount: 1, channelCountMode: "explicit", processorOptions: {frameSamples},
  });
  const gate = new VoiceSegments(handlers);
  let scoreFrame;
  let closed = false;
  let running = false;
  let captureEpoch = 0;
  // Capture while ONNX initializes, then work through the backlog in order.
  let queueLimit = maxFrames;
  const pending = [];

  function close() {
    if (closed) return;
    closed = true;
    pending.length = 0;
    node.port.onmessage = null;
    node.port.postMessage("SPEECH_STOP");
    node.port.close();
    source.disconnect();
    node.disconnect();
  }

  async function drain() {
    running = true;
    try {
      while (!closed && pending.length) {
        const frame = pending.shift();
        const epoch = captureEpoch;
        const score = await scoreFrame(frame);
        if (closed) return;
        if (epoch !== captureEpoch) continue;
        if (handlers.frame(frame)) gate.push(score, frame);
        else gate.suppress();
      }
    } catch (error) {
      if (!closed) handlers.error(error);
    } finally {
      running = false;
      if (!pending.length) queueLimit = steadyQueueFrames;
    }
  }

  node.port.onmessage = ({data}) => {
    if (closed || data?.message !== "AUDIO_FRAME") return;
    if (pending.length >= queueLimit) {
      handlers.error(new Error("Speech detection could not keep up with the microphone."));
      return;
    }
    pending.push(new Float32Array(data.data));
    if (scoreFrame && !running) void drain();
  };
  source.connect(node);
  // The worklet emits PCM messages and leaves its speaker output silent.
  node.connect(context.destination);
  void preloadVoiceInput().then(({ort, session}) => {
    if (closed) return;
    const rate = new ort.Tensor("int64", [16000n]);
    let state = new ort.Tensor("float32", new Float32Array(256), [2, 1, 128]);
    // Silero v5 takes the last 64 input samples followed by each new 512-sample frame.
    const inputSamples = new Float32Array(contextSamples + frameSamples);
    scoreFrame = async (frame) => {
      inputSamples.set(frame, contextSamples);
      const input = new ort.Tensor("float32", inputSamples, [1, inputSamples.length]);
      const result = await session.run({input, state, sr: rate});
      state = result.stateN;
      inputSamples.copyWithin(0, frameSamples);
      return result.output.data[0];
    };
    void drain();
  }).catch((error) => {
    if (!closed) handlers.error(error);
  });
  return {
    close,
    finish: () => gate.finish(),
    suppress() {
      captureEpoch++;
      pending.length = 0;
      gate.suppress();
    },
  };
}
