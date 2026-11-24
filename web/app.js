"use strict";

const $ = (id) => document.getElementById(id);
const audio = $("audio");
const device = $("device");
const voiceToggle = $("voice-toggle");
const canvas = $("waveform");
const ctx = canvas.getContext("2d");
const phases = ["asr", "chat", "tts"];
const phaseLabels = {queued: "等待上一轮退出", asr: "正在识别", chat: "正在生成回复", tts: "正在合成语音"};
const durationLimit = 15;
let mode = "idle";
let capture = null;
let voiceSession = null;
let previousPhase = "";
let holdingSpace = false;
let livePlayback = null;
let operation = null;
let cancellation = Promise.resolve();
let ttsMode = sessionStorage.getItem("voice-tts-mode") === "quality" ? "quality" : "fast";
let ttsSelectionAvailable = false;
let ttsSpeaker = globalThis.localStorage?.getItem("voice-tts-speaker") || "Vivian";
let qualityVoices = {};
let qualityPcm = false;
let qualityWarming = false;
let lastQualityWarmup = -Infinity;
let pcmContext, pcmModule;
let conversation = sessionStorage.getItem("voice-conversation");
if (!/^[0-9a-f]{32}$/.test(conversation || "")) newConversation();

function newConversation() {
  conversation = crypto.randomUUID().replaceAll("-", "");
  sessionStorage.setItem("voice-conversation", conversation);
}

function setMode(next) {
  mode = next;
  const active = next === "recording" || next === "auto-recording";
  $("record").disabled = next !== "idle" && !active && !operation?.interruptible;
  $("record").dataset.active = active;
  $("record-icon").setAttribute("href", active ? "#i-stop" : "#i-mic");
  $("record-text").textContent = active ? "停止录音" : next === "permission" ? "等待麦克风" : "开始录音";
  for (const id of ["device", "refresh", "upload", "sample", "new-chat"]) {
    $(id).disabled = next !== "idle";
  }
  device.disabled ||= !!voiceSession;
  $("tts-setting").disabled = next !== "idle";
  updateVoiceControls();
  for (const button of $("history").querySelectorAll("button")) button.disabled = next !== "idle";
  audio.controls = next === "idle" || (next === "processing" && !!livePlayback?.showingPart);
  $("monitor").dataset.active = active;
  const listening = voiceSession?.ready ? "自动监听中" : voiceSession ? "正在开启监听" : "待录音";
  $("input-state").textContent = active ? "录音中" : next === "permission" ? "等待授权" : next === "processing" ? "正在处理" : listening;
  updateInterrupt();
}

function updateInterrupt() {
  const canStopAudio = !audio.hidden && (!audio.paused || livePlayback?.playingPart);
  $("interrupt").disabled = !(operation?.interruptible || canStopAudio);
}

function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function updateVoiceControls() {
  const previewing = !!operation?.preview;
  const button = $("voice-preview");
  $("voice-preset").hidden = ttsMode !== "quality" || !Object.hasOwn(qualityVoices, ttsSpeaker);
  $("tts-speaker").disabled = mode !== "idle";
  button.hidden = !qualityPcm;
  button.disabled = mode !== "idle" && !previewing;
  button.title = previewing ? "停止试听" : "试听音色";
  button.setAttribute("aria-label", button.title);
  button.setAttribute("aria-pressed", String(previewing));
  $("voice-preview-icon").setAttribute("href", previewing ? "#i-stop" : "#i-play");
}

function setQualityVoices(voices = {}) {
  if (JSON.stringify(voices) === JSON.stringify(qualityVoices)) return;
  qualityVoices = voices;
  if (!Object.hasOwn(qualityVoices, ttsSpeaker)) ttsSpeaker = Object.keys(qualityVoices)[0] || "Vivian";
  const select = $("tts-speaker");
  select.replaceChildren(...Object.entries(qualityVoices).map(([id, label]) => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    return option;
  }));
  select.value = ttsSpeaker;
}

async function request(path, init = {}, room = conversation) {
  const headers = new Headers(init.headers);
  headers.set("X-Conversation-Id", room);
  const response = await fetch(path, {cache: "no-store", signal: AbortSignal.timeout(12000), ...init, headers});
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "本机服务未能处理请求。");
    error.status = response.status;
    throw error;
  }
  return result;
}

function keepQualityWarm() {
  if (!qualityPcm || ttsMode !== "quality" || qualityWarming ||
      (document.hidden && !voiceSession) || performance.now() - lastQualityWarmup < 120000) return;
  lastQualityWarmup = performance.now();
  void request("/api/warm-quality", {method: "POST"}).then((status) => {
    if (status.warming && ttsMode === "quality") $("connection-text").textContent = "Qwen 预热中";
  }).catch(() => { lastQualityWarmup = -Infinity; });
}

async function checkStatus() {
  let online = false;
  try {
    const status = await request("/api/status");
    online = status.llama;
    qualityPcm = !!status.quality_pcm;
    qualityWarming = !!status.quality_warming;
    if (status.tts_modes?.length && !status.tts_modes.includes(ttsMode)) ttsMode = status.tts_modes[0];
    ttsSelectionAvailable = status.tts_modes?.includes("fast") && status.tts_modes.includes("quality");
    $("tts-setting").hidden = !ttsSelectionAvailable;
    $("tts-fast").checked = ttsMode === "fast";
    $("tts-quality").checked = ttsMode === "quality";
    setQualityVoices(status.quality_voices);
    updateVoiceControls();
  } catch {
    // The indicator also covers an unreachable coordinator.
  }
  $("connection").dataset.online = online;
  $("connection-text").textContent = online
    ? (ttsMode === "quality" && qualityWarming ? "Qwen 预热中" : "模型在线") : "模型离线";
  if (online) keepQualityWarm();
}

async function refreshDevices(showError = false) {
  try {
    if (!navigator.mediaDevices) throw new Error();
    const list = await navigator.mediaDevices.enumerateDevices();
    const current = device.value;
    device.replaceChildren(new Option("系统默认麦克风", ""));
    const inputs = list.filter((item) => item.kind === "audioinput" && item.deviceId &&
      !["default", "communications"].includes(item.deviceId));
    for (const [index, item] of inputs.entries()) {
      device.appendChild(new Option(item.label || `麦克风 ${index + 1}`, item.deviceId));
    }
    if (inputs.some((item) => item.deviceId === current)) device.value = current;
  } catch {
    if (showError) notice("浏览器无法读取麦克风列表。请检查设备是否已连接。");
  }
}

function setTimer(seconds) {
  $("clock").textContent = `00:${String(Math.min(durationLimit, Math.floor(seconds))).padStart(2, "0")}`;
}

function draw(samples) {
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.beginPath();
  ctx.strokeStyle = samples ? "#167655" : "#91b5a2";
  ctx.lineWidth = Math.max(1, devicePixelRatio);
  if (!samples) {
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
  } else {
    for (let x = 0; x < w; x += 2) {
      const sample = samples[Math.floor(x / w * samples.length)];
      const normalized = Math.max(-1, Math.min(1, sample * 2.5));
      const y = h / 2 - normalized * h * 0.43;
      if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}

new ResizeObserver(() => {
  canvas.width = Math.max(1, Math.round(canvas.clientWidth * devicePixelRatio));
  canvas.height = Math.max(1, Math.round(canvas.clientHeight * devicePixelRatio));
  draw();
}).observe(canvas);

function setLevel(level) {
  $("meter").value = level;
  $("level").textContent = `${Math.round(level * 100)}%`;
}

function monitorCapture(current) {
  if (mode !== "recording" || current !== capture) return;
  const elapsed = (performance.now() - current.started) / 1000;
  setTimer(elapsed);
  if (current.analyser) {
    current.analyser.getFloatTimeDomainData(current.samples);
    let squares = 0;
    for (const sample of current.samples) squares += sample * sample;
    setLevel(Math.min(1, Math.sqrt(squares / current.samples.length) * 5));
    draw(current.samples);
  }
  current.frame = requestAnimationFrame(() => monitorCapture(current));
}

function stopTracks(stream) {
  stream?.getTracks().forEach((track) => track.stop());
}

function cleanCapture(current) {
  clearTimeout(current.stopTimer);
  cancelAnimationFrame(current.frame);
  stopTracks(current.stream);
  current.context?.close().catch(() => {});
  if (capture === current) capture = null;
  setLevel(0);
  draw();
}

function micError(error) {
  switch (error.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "浏览器未获得麦克风权限。请检查地址栏的站点权限和 Windows 麦克风权限。";
    case "NotFoundError":
    case "OverconstrainedError":
      return "找不到所选麦克风。请刷新设备列表，或改选系统默认麦克风。";
    case "NotReadableError":
    case "AbortError":
      return "浏览器无法打开麦克风。设备可能被占用，请试用其他输入或上传音频。";
    default:
      return error.message || "无法开始录音。";
  }
}

function stopVoiceInput() {
  const current = voiceSession;
  voiceSession = null;
  voiceToggle.checked = false;
  current?.engine?.close();
  stopTracks(current?.stream);
  current?.context?.close().catch(() => {});
  if (mode === "auto-recording" || mode === "idle") {
    setLevel(0);
    draw();
  }
  setMode(mode === "auto-recording" ? "idle" : mode);
}

async function startVoiceInput() {
  preparePcm();
  const current = voiceSession = {ready: false};
  setMode(mode);
  notice();
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      throw new Error("当前浏览器不支持自动收音，请使用 Edge 或 Chrome。");
    }
    const context = current.context = new AudioContext();
    const selected = device.value;
    const inputModule = import("/voice-input.mjs");
    const [module, stream] = await Promise.all([
      inputModule,
      navigator.mediaDevices.getUserMedia({audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        ...(selected ? {deviceId: {exact: selected}} : {}),
      }}).then((stream) => {
        if (voiceSession === current) current.stream = stream; else stopTracks(stream);
        return stream;
      }),
      context.resume(),
      inputModule.then((module) => module.prepareVoiceInput(context)),
    ]);
    if (voiceSession !== current) return;
    const failed = () => {
      if (voiceSession !== current) return;
      stopVoiceInput();
      notice("自动收音已停止，请检查麦克风后重新开启。");
    };
    const engine = module.connectVoiceInput(context, stream, {
      playing: () => !!(livePlayback?.pcmPlaying || operation?.previewState?.pcmPlaying)
        || (operation?.previewAudio && !operation.previewAudio.paused)
        || (!audio.hidden && !audio.paused && !audio.ended),
      frame(samples) {
        if (voiceSession !== current || !current.ready || mode === "permission" || mode === "recording") return false;
        if (mode === "auto-recording") {
          setTimer((performance.now() - current.started) / 1000);
          let sum = 0;
          for (const sample of samples) sum += sample * sample;
          setLevel(Math.min(1, Math.sqrt(sum / samples.length) * 5));
          draw(samples);
        }
        return true;
      },
      start() {
        if (voiceSession !== current || (mode !== "idle" && !operation?.interruptible)) return false;
        interrupt(false);
        current.started = performance.now();
        setTimer(0);
        notice();
        setMode("auto-recording");
        return true;
      },
      end(samples) {
        if (voiceSession !== current || mode !== "auto-recording") return;
        const endedAt = performance.now();
        setLevel(0);
        draw();
        void runOperation((turn) => submit(turn, "/api/turn", encodeWav(samples), endedAt));
      },
      error: failed,
    });
    if (voiceSession !== current) {
      engine.close();
      return;
    }
    current.engine = engine;
    current.ready = true;
    for (const track of stream.getAudioTracks()) track.addEventListener("ended", failed, {once: true});
    if (!stream.active) { failed(); return; }
    setMode(mode);
    void refreshDevices();
  } catch (error) {
    if (voiceSession !== current) return;
    stopVoiceInput();
    notice(error.name === "Error" && current.stream ? "无法加载本地语音检测，请关闭后重试。" : micError(error));
  }
}

async function startRecording(byKey = false) {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    notice("当前浏览器不支持录音。请用本机 Edge 或 Chrome 打开此页面。");
    return;
  }
  preparePcm();
  const selected = device.value;
  if (voiceSession && !voiceSession.ready) stopVoiceInput();
  voiceSession?.engine?.suppress();
  const sharedStream = voiceSession?.stream;
  livePlayback = null;
  audio.pause();
  setMode("permission");
  notice();
  setTimer(0);
  let stream;
  try {
    stream = sharedStream?.clone() || await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, ...(selected ? {deviceId: {exact: selected}} : {})},
    });
    if (byKey && !holdingSpace) {
      stopTracks(stream);
      setMode("idle");
      void refreshDevices();
      return;
    }
    const recorder = new MediaRecorder(stream);
    const current = {stream, recorder, byKey, chunks: [], started: performance.now(), failed: false};
    capture = current;
    recorder.ondataavailable = ({data}) => { if (data.size) current.chunks.push(data); };
    recorder.onerror = (event) => {
      current.failed = true;
      cleanCapture(current);
      setMode("idle");
      notice(micError(event.error || new Error("录音中断。")));
    };
    recorder.onstop = async () => {
      if (current.failed) return;
      setTimer((performance.now() - current.started) / 1000);
      cleanCapture(current);
      const clip = new Blob(current.chunks, {type: recorder.mimeType});
      await processFile(clip, true, current.stoppedAt || performance.now());
    };
    recorder.start();
    setMode("recording");
    current.stopTimer = setTimeout(stopRecording, durationLimit * 1000);
    try {
      const context = current.context = new AudioContext();
      current.analyser = context.createAnalyser();
      current.analyser.fftSize = 1024;
      current.samples = new Float32Array(current.analyser.fftSize);
      context.createMediaStreamSource(stream).connect(current.analyser);
      context.resume().catch(() => {});
    } catch {
      current.analyser = null;
    }
    monitorCapture(current);
    void refreshDevices();
  } catch (error) {
    stopTracks(stream);
    capture = null;
    setMode("idle");
    notice(micError(error));
  }
}

function stopRecording() {
  if (!capture || capture.recorder.state !== "recording") return;
  voiceSession?.engine?.suppress();
  capture.stoppedAt = performance.now();
  capture.recorder.stop();
  setMode("processing");
}

async function pcmWav(file, recorded) {
  if (!file.size || file.size > 20 * 1024 * 1024) throw new Error("音频为空，或文件超过 20 MB。");
  const context = new AudioContext();
  let decoded;
  try {
    decoded = await context.decodeAudioData(await file.arrayBuffer());
  } catch {
    throw new Error("浏览器无法解码这段音频。请试用 WAV、MP3 或 WebM。");
  } finally {
    await context.close();
  }
  if (decoded.duration < 0.3 || (!recorded && decoded.duration > durationLimit)) {
    throw new Error("音频时长须为 0.3 至 15 秒。");
  }
  const rate = 16000;
  const length = Math.min(durationLimit * rate, Math.floor(decoded.duration * rate));
  const offline = new OfflineAudioContext(1, length, rate);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const samples = (await offline.startRendering()).getChannelData(0);
  return encodeWav(samples, recorded);
}

function encodeWav(samples, recorded = false) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  function writeText(offset, text) {
    [...text].forEach((character, i) => view.setUint8(offset + i, character.charCodeAt(0)));
  }
  writeText(0, "RIFF");
  view.setUint32(4, buffer.byteLength - 8, true);
  writeText(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    peak = Math.max(peak, Math.abs(sample));
    view.setInt16(44 + i * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  if (recorded && peak < 0.004) throw new Error("录音几乎没有声音。请检查输入电平、静音开关或换一个麦克风。");
  return buffer;
}

function showText(id, value, placeholder) {
  const node = $(id);
  if (node.textContent !== (value || placeholder)) node.textContent = value || placeholder;
  node.classList.toggle("placeholder", !value);
}

function showStage(phase) {
  const index = phase === "done" ? 3 : phases.indexOf(phase);
  for (const [position, node] of [...document.querySelectorAll(".steps li")].entries()) {
    node.dataset.state = position < index ? "complete" : position === index ? "active" : "";
    node.firstElementChild.textContent = position < index ? "✓" : String(position + 1);
    if (position === index) node.setAttribute("aria-current", "step"); else node.removeAttribute("aria-current");
  }
  previousPhase = phase;
}

function clearAudio() {
  livePlayback?.stopPcm?.();
  livePlayback = null;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  audio.hidden = true;
  $("download").hidden = true;
  $("no-audio").hidden = false;
  updateInterrupt();
}

function clearResult() {
  clearAudio();
  showText("transcript", "", "等待音频");
  showText("answer", "", "等待回复");
  $("turn-status").textContent = "";
  $("first-audio").textContent = "";
  $("timing").textContent = "";
  showStage("");
}

function audioFor(id, name = "reply.wav") {
  return `/api/turn/${encodeURIComponent(id)}/${name}?conversation=${conversation}`;
}

function showAudioSource(src) {
  if (new URL(src, location.href).href !== audio.src) audio.src = src;
  audio.hidden = false;
  $("no-audio").hidden = true;
}

function showDownload(id) {
  $("download").href = audioFor(id);
  $("download").hidden = false;
}

function showAudio(id) {
  showAudioSource(audioFor(id));
  showDownload(id);
}

function preparePcm() {
  if (!qualityPcm || ttsMode !== "quality" || !window.AudioWorkletNode) return;
  try {
    pcmContext ??= new AudioContext({sampleRate: 24000});
    pcmModule ??= pcmContext.audioWorklet.addModule("/pcm-output.js").catch((error) => {
      pcmModule = null;
      throw error;
    });
    void pcmModule.catch(() => {});
    void pcmContext.resume().catch(() => {});
  } catch {
    // The completed WAV is still available if the browser cannot stream PCM.
  }
}

function markPlayback(state) {
  if (state !== livePlayback || state.startedPlayback || state.startedAt === undefined) return;
  state.startedPlayback = true;
  $("first-audio").textContent = `首段开始播放 · ${((performance.now() - state.startedAt) / 1000).toFixed(2)} 秒`;
}

async function playPcm(current, state, onStart = () => {
  if (state !== livePlayback) return;
  $("no-audio").hidden = true;
  markPlayback(state);
}) {
  const controller = new AbortController();
  let node, finish;
  const drained = new Promise((resolve) => { finish = resolve; });
  state.stopPcm = () => {
    controller.abort();
    node?.disconnect();
    node?.port.close();
    state.pcmPlaying = false;
    finish();
  };
  try {
    if (!pcmModule) throw new Error("PCM worklet unavailable");
    await pcmModule;
    if (controller.signal.aborted) return;
    if (pcmContext.state !== "running" || pcmContext.sampleRate !== 24000) {
      throw new Error("24 kHz playback not available");
    }
    node = new AudioWorkletNode(pcmContext, "voice-pcm-output", {numberOfInputs: 0, outputChannelCount: [1]});
    node.onprocessorerror = () => {
      state.pcmFailed = true;
      state.stopPcm();
    };
    node.port.onmessage = ({data}) => {
      if (controller.signal.aborted) return;
      if (data === "started") {
        state.pcmPlaying = true;
        onStart();
      } else if (data === "ended") {
        state.pcmPlaying = false;
        finish();
      } else if (data === "underrun") {
        state.underruns = (state.underruns || 0) + 1;
      }
    };
    node.connect(pcmContext.destination);
    const response = await fetch(`/api/turn/${encodeURIComponent(state.id)}/stream.pcm`, {
      signal: controller.signal, cache: "no-store", headers: {"X-Conversation-Id": current.room},
    });
    if (!response.ok || !response.body) throw new Error("PCM response unavailable");
    const reader = response.body.getReader();
    let leftover;
    try {
      while (!controller.signal.aborted) {
        const {value, done} = await reader.read();
        if (done || controller.signal.aborted) break;
        // HTTP may split a 16-bit sample across two reads.
        const lead = leftover === undefined ? 0 : 1;
        const size = value.length + lead;
        const samples = new Uint8Array(size - (size % 2));
        if (lead && samples.length) samples[0] = leftover;
        samples.set(value.subarray(0, Math.max(0, samples.length - lead)), Math.min(lead, samples.length));
        leftover = size % 2 ? (value.length ? value[value.length - 1] : leftover) : undefined;
        if (samples.length) {
          state.pcmReceived = (state.pcmReceived || 0) + samples.length;
          node.port.postMessage(samples.buffer, [samples.buffer]);
        }
      }
    } finally {
      reader.releaseLock();
    }
    if (controller.signal.aborted) return;
    if (leftover !== undefined) throw new Error("Incomplete PCM sample");
    node.port.postMessage("end");
    await drained;
  } catch {
    if (!controller.signal.aborted) state.pcmFailed = true;
  } finally {
    state.stopPcm();
  }
}

function advancePlayback(state) {
  if (state !== livePlayback || state.playingPart || state.fullReady) return;
  if (!state.failed && state.next < state.available) {
    state.playingPart = true;
    state.showingPart = true;
    showAudioSource(audioFor(state.id, `part-${state.next++}.wav`));
    audio.controls = true;
    void audio.play().catch(() => {});
  } else if (state.done) {
    state.fullReady = true;
    state.showingPart = false;
    showAudio(state.id);
    if (!state.available) void audio.play().catch(() => {});
  }
  updateInterrupt();
}

function showJobAudio(job) {
  const state = livePlayback;
  if (!state || state.id !== job.id) {
    if (job.phase === "done") showAudio(job.id);
    return;
  }
  state.available = job.parts || 0;
  state.done = job.phase === "done";
  state.pcmExpected = job.pcm_bytes;
  if (state.done) showDownload(job.id);
  if (!state.pcm) advancePlayback(state);
}

audio.addEventListener("ended", () => {
  if (livePlayback?.playingPart && audio.ended) {
    livePlayback.playingPart = false;
    advancePlayback(livePlayback);
  }
  updateInterrupt();
});
audio.addEventListener("play", updateInterrupt);
audio.addEventListener("pause", updateInterrupt);
audio.addEventListener("playing", () => {
  if (livePlayback) markPlayback(livePlayback);
});

function showHistory(turns, currentId) {
  const history = $("history");
  history.replaceChildren();
  for (const turn of [...turns].reverse().filter((item) => item.id !== currentId)) {
    const entry = document.createElement("li");
    entry.className = "history-item";
    const text = document.createElement("div");
    for (const [label, value] of [["你", turn.transcript], ["助手", turn.answer]]) {
      const line = document.createElement("p");
      const name = document.createElement("strong");
      name.textContent = label;
      line.append(name, document.createTextNode(value));
      text.append(line);
    }
    const play = document.createElement("button");
    play.type = "button";
    play.className = "icon-button";
    play.title = "播放此轮回复";
    play.setAttribute("aria-label", play.title);
    play.disabled = mode !== "idle";
    play.innerHTML = '<svg aria-hidden="true"><use href="#i-play"></use></svg>';
    play.onclick = () => {
      livePlayback = null;
      showAudio(turn.id);
      audio.play().catch((error) => {
        if (!audio.hidden && error.name !== "AbortError") notice("无法播放这条回复。");
      });
    };
    entry.append(text, play);
    history.append(entry);
  }
  $("history-panel").hidden = !history.childElementCount;
}

function showJob(job) {
  if (job.phase === "cancelled") {
    clearResult();
    $("turn-status").textContent = "已打断";
    return;
  }
  showText("transcript", job.transcript, "等待转写");
  showText("answer", job.answer, "等待回复");
  if (job.phase === "error") {
    clearAudio();
    $("first-audio").textContent = "";
    throw new Error(job.error || "处理失败。");
  }
  if (job.phase !== previousPhase) showStage(job.phase);
  const status = job.phase === "done" ? `完成 · ${job.seconds.toFixed(1)} 秒` :
    job.phase === "chat" && job.parts ? "正在生成 · 语音已就绪" : phaseLabels[job.phase] || "";
  if ($("turn-status").textContent !== status) $("turn-status").textContent = status;
  showJobAudio(job);
  if (job.first_audio !== undefined && !livePlayback?.startedPlayback) {
    $("first-audio").textContent = `首段就绪 · ${job.first_audio.toFixed(1)} 秒`;
  }
  if (job.phase === "done") {
    const timings = [["识别", job.timings.asr], ["首字", job.first_text], ["问答", job.timings.chat], ["合成", job.timings.tts]];
    $("timing").textContent = timings.filter(([, seconds]) => Number.isFinite(seconds))
      .map(([label, seconds]) => `${label} ${seconds.toFixed(1)} 秒`).join(" · ");
  }
}

function readOperation(current, path) {
  const signal = AbortSignal.any([current.poll.signal, AbortSignal.timeout(12000)]);
  return request(path, {signal}, current.room);
}

async function trackTurn(current) {
  while (operation === current) {
    const job = await readOperation(current, `/api/turn/${encodeURIComponent(current.id)}`);
    if (operation !== current) return;
    showJob(job);
    if (job.phase === "done" || job.phase === "cancelled") return;
    await new Promise((resolve) => setTimeout(resolve, job.phase === "chat" ? 150 : 350));
  }
}

async function refreshHistory(current) {
  const room = await readOperation(current, "/api/conversation");
  if (operation === current) showHistory(room.turns, current.id);
}

async function submit(current, path, body, startedAt) {
  await cancellation;
  if (operation !== current) return;
  clearResult();
  const headers = {};
  if (body) headers["Content-Type"] = "audio/wav";
  if (current.ttsMode) headers["X-Voice-TTS"] = current.ttsMode;
  if (current.speaker) headers["X-Voice-Speaker"] = current.speaker;
  const init = {method: "POST", body: body || undefined, headers};
  // Keep the POST alive so an interrupt can still cancel the assigned turn.
  current.submission = request(path, init, current.room);
  const {id, pcm} = await current.submission;
  if (operation !== current) return;
  current.id = id;
  const state = livePlayback = {id, startedAt, next: 0, available: 0, pcm};
  if (pcm) {
    const playback = playPcm(current, state);
    try {
      await trackTurn(current);
      await playback;
      if (operation === current && state === livePlayback && state.done) {
        showAudio(id);
        if (!state.startedPlayback) void audio.play().catch(() => {});
        else if (state.pcmFailed || state.pcmReceived !== state.pcmExpected) {
          notice("实时音频传输中断，可重播完整回复。");
        }
      }
    } finally {
      state.stopPcm();
    }
  } else {
    await refreshHistory(current);
    await trackTurn(current);
  }
  if (operation === current) await refreshHistory(current);
}

function processingFailed(error) {
  $("turn-status").textContent = "未完成";
  notice(error.name === "TimeoutError" ? "本机请求超时，请稍后重试。" : error.message || "处理失败。");
}

async function runOperation(work, interruptible = true) {
  const current = {room: conversation, poll: new AbortController(), interruptible,
    ttsMode: ttsSelectionAvailable ? ttsMode : null,
    speaker: ttsMode === "quality" && Object.hasOwn(qualityVoices, ttsSpeaker) ? ttsSpeaker : null};
  operation = current;
  livePlayback = null;
  audio.pause();
  setMode("processing");
  try {
    await work(current);
  } catch (error) {
    if (operation === current) processingFailed(error);
  } finally {
    if (operation === current) {
      operation = null;
      setMode("idle");
    }
  }
}

function processFile(file, recorded, startedAt = performance.now()) {
  preparePcm();
  notice();
  return runOperation(async (current) => {
    await submit(current, "/api/turn", await pcmWav(file, recorded), startedAt);
  });
}

async function trackPreview(current) {
  while (operation === current) {
    const job = await readOperation(current, `/api/turn/${current.id}`);
    if (job.phase === "error") throw new Error(job.error);
    if (job.phase === "done" || job.phase === "cancelled") return job;
    await new Promise((resolve) => setTimeout(resolve, 125));
  }
}

function playPreviewWav(current) {
  const player = current.previewAudio = new Audio(
    `/api/turn/${current.id}/reply.wav?conversation=${current.room}`);
  return new Promise((resolve, reject) => {
    current.stopPreviewWav = () => {
      player.pause();
      player.removeAttribute("src");
      resolve();
    };
    player.onended = resolve;
    player.onerror = () => reject(new Error("无法播放试听音频。"));
    player.play().catch(reject);
  });
}

async function previewVoice() {
  if (mode !== "idle" || ttsMode !== "quality" || !qualityPcm || !Object.hasOwn(qualityVoices, ttsSpeaker)) return;
  preparePcm();
  const current = {room: conversation, poll: new AbortController(), interruptible: true, preview: true};
  const speaker = ttsSpeaker;
  operation = current;
  audio.pause();
  notice();
  setMode("processing");
  try {
    await cancellation;
    if (operation !== current) return;
    current.submission = request("/api/voice-preview", {
      method: "POST", headers: {"X-Voice-Speaker": speaker},
    }, current.room);
    const {id, room} = await current.submission;
    if (operation !== current) return;
    Object.assign(current, {id, room});
    const state = current.previewState = {id};
    const playback = playPcm(current, state, () => { state.startedPlayback = true; });
    const job = await trackPreview(current);
    await playback;
    if (operation !== current || job?.phase !== "done") return;
    if (!state.startedPlayback) await playPreviewWav(current);
    else if (state.pcmFailed || state.pcmReceived !== job.pcm_bytes) notice("试听中断，请重试。");
  } catch (error) {
    if (operation === current) notice(error.message || "试听失败。");
  } finally {
    current.previewState?.stopPcm?.();
    current.stopPreviewWav?.();
    if (operation === current) {
      operation = null;
      setMode("idle");
    }
  }
}

function cancelTurnRequest(turn) {
  if (!turn?.id && !turn?.submission) return;
  cancellation = (async () => {
    const submitted = turn.id ? null : await turn.submission.catch(() => null);
    const id = turn.id || submitted?.id;
    if (id) await request(`/api/turn/${encodeURIComponent(id)}/cancel`, {method: "POST"}, submitted?.room || turn.room);
  })().catch((error) => {
    if (error.status !== 404 && mode === "idle" && (turn.preview || conversation === turn.room)) {
      notice("本机服务未确认打断，请刷新页面检查。");
    }
  });
}

function interrupt(moveFocus = true) {
  if ($("interrupt").disabled) return false;
  const current = operation?.interruptible ? operation : null;
  const turn = current || (livePlayback && {id: livePlayback.id, room: conversation});
  operation = null;
  current?.poll.abort();
  if (current?.preview) {
    current.previewState?.stopPcm?.();
    current.stopPreviewWav?.();
    setMode("idle");
    cancelTurnRequest(current);
    if (moveFocus) $("voice-preview").focus();
    return true;
  }
  if (turn) clearResult(); else clearAudio();
  $("turn-status").textContent = "已打断";
  notice();
  setMode("idle");
  if (moveFocus) $("record").focus();
  cancelTurnRequest(turn);
  return true;
}

$("interrupt").onclick = () => interrupt();
$("tts-speaker").onchange = () => {
  const value = $("tts-speaker").value;
  if (!Object.hasOwn(qualityVoices, value)) return;
  ttsSpeaker = value;
  globalThis.localStorage?.setItem("voice-tts-speaker", value);
};
$("voice-preview").onclick = () => {
  if (operation?.preview) interrupt();
  else void previewVoice();
};
for (const status of ["fast", "quality"]) {
  const option = $(`tts-${status}`);
  option.onchange = () => {
    if (!option.checked) return;
    ttsMode = status;
    sessionStorage.setItem("voice-tts-mode", status);
    updateVoiceControls();
    if (status === "quality" && qualityPcm) {
      preparePcm();
      lastQualityWarmup = -Infinity;
      keepQualityWarm();
    }
  };
}
voiceToggle.checked = false;
voiceToggle.onchange = () => {
  if (voiceToggle.checked && !voiceSession) void startVoiceInput();
  else stopVoiceInput();
};
$("record").onclick = () => {
  if (mode === "auto-recording") voiceSession?.engine?.finish();
  else if (mode === "recording") stopRecording();
  else if (mode === "idle" || (operation?.interruptible && interrupt(false))) void startRecording();
};
$("new-chat").onclick = () => {
  if (mode !== "idle") return;
  voiceSession?.engine?.suppress();
  newConversation();
  clearResult();
  showHistory([]);
  notice();
  setTimer(0);
};
$("refresh").onclick = () => { notice(); void refreshDevices(true); };
$("upload").onclick = () => $("file").click();
$("file").onchange = () => {
  const [file] = $("file").files;
  $("file").value = "";
  if (file && mode === "idle") { setTimer(0); void processFile(file, false); }
};
$("sample").onclick = () => {
  if (mode !== "idle") return;
  preparePcm();
  notice();
  setTimer(0);
  void runOperation((current) => submit(current, "/api/sample", null, performance.now()));
};
audio.onerror = () => {
  if (audio.hidden || !audio.error) return;
  if (livePlayback?.playingPart) {
    livePlayback.failed = true;
    livePlayback.playingPart = false;
    advancePlayback(livePlayback);
  }
  notice("浏览器无法播放这段回复。可重新测试或下载音频。");
};
navigator.mediaDevices?.addEventListener("devicechange", () => { void refreshDevices(); });
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !event.repeat && !event.isComposing && interrupt()) {
    event.preventDefault();
    return;
  }
  if (event.code !== "Space" || event.ctrlKey || event.altKey || event.metaKey) return;
  if (holdingSpace) { event.preventDefault(); return; }
  if (event.repeat || (mode !== "idle" && !operation?.interruptible)) return;
  if (event.target !== $("record") && event.target instanceof Element &&
      event.target.closest("button, a, input, select, textarea, audio, [contenteditable]")) return;
  event.preventDefault();
  if (operation?.interruptible) interrupt(false);
  holdingSpace = true;
  void startRecording(true);
});
function releaseSpace() {
  if (!holdingSpace) return;
  holdingSpace = false;
  if (capture?.byKey) stopRecording();
}
window.addEventListener("keyup", (event) => {
  if (event.code === "Space" && holdingSpace) {
    event.preventDefault();
    releaseSpace();
  }
});
window.addEventListener("blur", releaseSpace);
window.addEventListener("pagehide", () => {
  operation?.previewState?.stopPcm?.();
  operation?.stopPreviewWav?.();
  livePlayback?.stopPcm?.();
  if (voiceSession) stopVoiceInput();
  if (capture) {
    capture.failed = true;
    cleanCapture(capture);
  }
});
function restoreConversation() {
  return runOperation(async (current) => {
    const room = await readOperation(current, "/api/conversation");
    if (operation !== current) return;
    const job = room.latest;
    current.id = job?.id;
    showHistory(room.turns, current.id);
    if (!job) return;
    // A restored turn can finish, but should only be played by explicit user action.
    showJob(job);
    if (job.phase !== "done" && job.phase !== "cancelled") {
      current.interruptible = true;
      updateInterrupt();
      await trackTurn(current);
      if (operation === current) await refreshHistory(current);
    }
  }, false);
}
void checkStatus();
void refreshDevices();
void restoreConversation();
if (window.AudioWorkletNode && navigator.mediaDevices?.getUserMedia) {
  void import("/voice-input.mjs").then((module) => module.preloadVoiceInput()).catch(() => {});
}
setInterval(checkStatus, 7000);
