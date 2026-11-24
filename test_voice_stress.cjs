"use strict";

const assert = require("node:assert/strict");
const {mkdirSync, readFileSync, writeFileSync} = require("node:fs");
const {join, resolve} = require("node:path");
const {spawn, spawnSync} = require("node:child_process");
const {once} = require("node:events");
const {setTimeout: delay} = require("node:timers/promises");
const {chromium} = require("playwright");

const prompts = [
  "我叫小林，请记住我的名字。",
  "请用一句话介绍上海。",
  "北京的冬天通常冷吗？",
  "请推荐一个健康的早餐。",
  "请说出我的名字。",
  "请列举三种适合室内的运动。",
  "风是怎么形成的？",
  "请推荐一道简单的家常菜。",
  "我坐高铁出门，需要带什么？",
  "请给我一句睡前祝福。",
  "请详细介绍上海两日游，分五点，每点多说一点。",
  "换个话题，苹果是什么颜色的？",
  "先不说旅游，月亮为什么会发光？",
  "请告诉我，你还记得我的名字吗？",
  "请详细介绍南方和北方的饮食，有哪些不同？",
  "请用一句话说说什么是春天。",
];
const url = process.env.VOICE_URL || "http://127.0.0.1:8010";
const root = resolve(process.env.VOICE_ROOT || ".");
const output = join(__dirname, "voice-stress-results", new Date().toISOString().replaceAll(":", "-"));
const round = (ms) => Math.round(ms) / 1000;

function fixtures() {
  mkdirSync(output, {recursive: true});
  const generated = spawnSync(join(root, ".venv-fasttts", "Scripts", "python.exe"), ["-X", "utf8", "-c", `
import json, sys
from pathlib import Path
from fast_tts import PiperTTS
voice = PiperTTS()
for i, line in enumerate(json.load(sys.stdin)):
    voice.synthesize(line, Path(sys.argv[1]) / f"{i}.wav")
`, output], {cwd: root, input: JSON.stringify(prompts), encoding: "utf8", windowsHide: true});
  assert.equal(generated.status, 0, generated.error?.message || generated.stderr);
  return prompts.map((_, i) => readFileSync(join(output, `${i}.wav`)).toString("base64"));
}

function injectMic() {
  let context;
  navigator.mediaDevices.getUserMedia = async () => {
    context = new AudioContext();
    const destination = context.createMediaStreamDestination();
    await context.resume();
    window.stressDestination = destination;
    return destination.stream;
  };
  window.stressSpeak = async (data) => {
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    const buffer = await context.decodeAudioData(bytes.buffer);
    const wave = buffer.getChannelData(0);
    let tail = wave.length - 1;
    while (tail > 0 && Math.abs(wave[tail]) < 0.008) tail--;
    const duration = Math.min(buffer.duration, tail / buffer.sampleRate + 0.065);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(window.stressDestination);
    const lead = 0.15;
    const now = performance.now();
    const attempt = window.stressAttempt = {beganAt: now + lead * 1000, endAt: now + (lead + duration) * 1000};
    source.start(context.currentTime + lead, 0, duration);
    source.onended = () => { attempt.endedEventAt = performance.now(); };
  };
}

function observeUi() {
  new MutationObserver(() => {
    const row = window.stressAttempt;
    if (!row || !livePlayback || livePlayback.startedAt < row.beganAt) return;
    row.utteranceSubmittedAt ??= livePlayback.startedAt;
    const present = (id) => !document.getElementById(id).classList.contains("placeholder")
      && document.getElementById(id).textContent.trim();
    if (present("transcript")) row.transcriptAt ??= performance.now();
    if (present("answer")) row.firstTextAt ??= performance.now();
    if (livePlayback.startedPlayback) row.playAt ??= performance.now();
  }).observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true});
}

async function main() {
  const waves = fixtures();
  const report = {
    url, data: output, completed: [], interruptions: [], reconnect: [],
    memoryChecks: [], busyRetries: [], errors: [], gpu: [],
  };
  const reconnectOnly = process.argv.includes("--reconnect-only");
  const gpu = spawn("nvidia-smi", [
    "--query-gpu=memory.total,memory.used,memory.free", "--format=csv,noheader,nounits", "-lms", "200",
  ], {windowsHide: true});
  let lastLine = "";
  gpu.stdout.setEncoding("utf8").on("data", (chunk) => {
    const lines = (lastLine + chunk).split(/\r?\n/);
    lastLine = lines.pop();
    for (const line of lines) {
      if (/^\d+, \d+, \d+$/.test(line)) report.gpu.push(line.split(",").map(Number));
    }
  });
  gpu.on("error", (e) => report.errors.push(e.message));
  let browser;
  try {
    browser = await chromium.launch({executablePath: process.env.EDGE_PATH});
    const context = await browser.newContext();
    await context.addInitScript(injectMic);
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on("pageerror", (e) => report.errors.push(e.message));
    await page.goto(url);
    const idle = () => page.waitForFunction(() => mode === "idle");
    const api = (path) => page.evaluate(async (route) => {
      const response = await fetch(route, {
        headers: {"X-Conversation-Id": sessionStorage.getItem("voice-conversation")},
      });
      return {code: response.status, body: await response.json()};
    }, path);
    const room = () => api("/api/conversation").then((response) => response.body);
    const job = (id) => api(`/api/turn/${id}`).then((response) => response.body);
    const roomId = () => page.evaluate(() => sessionStorage.getItem("voice-conversation"));

    async function enableInput() {
      await idle();
      await page.locator("#tts-quality + span").click();
      await page.locator("label:has(#voice-toggle)").click();
      await page.waitForFunction(() => voiceSession?.ready && mode === "idle");
      await page.evaluate(observeUi);
    }

    async function say(index, attempt = 1) {
      const upload = page.waitForResponse((response) =>
        response.url().endsWith("/api/turn") && response.request().method() === "POST");
      await page.evaluate((fixture) => window.stressSpeak(fixture), waves[index]);
      const response = await upload;
      if (response.status() === 409 && attempt < 4) {
        report.busyRetries.push({index, attempt});
        await idle();
        await delay(1200);
        return say(index, attempt + 1);
      }
      assert.equal(response.status(), 202, await response.text());
      const {id, pcm} = await response.json();
      assert.equal(pcm, true);
      await page.waitForFunction((id) => livePlayback?.id === id && livePlayback.startedPlayback, id);
      return id;
    }

    async function collect(index, id, label) {
      await idle();
      const turn = await job(id);
      assert.equal(turn.phase, "done", JSON.stringify(turn));
      assert.equal(turn.tts_backend, "qwen-graph");
      const client = await page.evaluate(() => ({
        ...window.stressAttempt, playingId: livePlayback?.id, received: livePlayback?.pcmReceived,
        underruns: livePlayback?.underruns || 0, badStream: !!livePlayback?.pcmFailed,
      }));
      assert.equal(client.playingId, id);
      assert.equal(client.received, turn.pcm_bytes);
      assert.equal(client.badStream, false);
      assert.ok([client.transcriptAt, client.firstTextAt, client.playAt].every(Number.isFinite));
      const row = {
        label, id, prompt: prompts[index], transcript: turn.transcript, answer: turn.answer,
        vadEnd_s: round(client.utteranceSubmittedAt - client.endAt),
        transcript_s: round(client.transcriptAt - client.endAt),
        firstText_s: round(client.firstTextAt - client.endAt),
        playback_s: round(client.playAt - client.endAt),
        submitToPlayback_s: round(client.playAt - client.utteranceSubmittedAt),
        serverAsr_s: turn.timings.asr, serverFirstText_s: turn.first_text, serverFirstPcm_s: turn.first_audio,
        pcm_bytes: turn.pcm_bytes, underruns: client.underruns,
      };
      report.completed.push(row);
      console.log(JSON.stringify(row));
      return row;
    }

    async function cancelled(id, kind) {
      const result = await job(id);
      assert.equal(result.phase, "cancelled");
      assert.equal((await room()).turns.some((turn) => turn.id === id), false);
      const archive = await page.request.get(`${url}/api/turn/${id}/reply.wav?conversation=${await roomId()}`);
      assert.equal(archive.status(), 404);
      report.interruptions.push({kind, id, historyExcluded: true, audioInaccessible: true});
      console.log(JSON.stringify(report.interruptions.at(-1)));
    }

    await enableInput();
    if (!reconnectOnly) {
      for (let i = 0; i < 10; i++) {
        const result = await collect(i, await say(i), i === 0 ? "first-browser-turn" : "continuous");
        if (i === 4) report.memoryChecks.push({id: result.id, recall: /小林/u.test(result.answer)});
      }
      assert.equal((await room()).turns.length, 10);

      for (const index of [11, 12]) {
        const old = await say(10);
        assert.equal(await page.evaluate(() => livePlayback?.pcmPlaying), true);
        const replacement = await say(index);
        assert.notEqual(replacement, old);
        await collect(index, replacement, "voice-interrupt-followup");
        // Replaced cancelled jobs are no longer addressable by turn ID.
        assert.equal((await api(`/api/turn/${old}`)).code, 404);
        assert.equal((await room()).turns.some((turn) => turn.id === old), false);
        const archive = await page.request.get(`${url}/api/turn/${old}/reply.wav?conversation=${await roomId()}`);
        assert.equal(archive.status(), 404);
        report.interruptions.push({kind: "voice", id: old, replacement, historyExcluded: true, audioInaccessible: true});
        console.log(JSON.stringify(report.interruptions.at(-1)));
      }

      const old = await say(10);
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => livePlayback === null && mode === "idle");
      await page.waitForFunction(async (id) => {
        const response = await fetch(`/api/turn/${id}`, {
          headers: {"X-Conversation-Id": sessionStorage.getItem("voice-conversation")},
        });
        return (await response.json()).phase === "cancelled";
      }, old);
      await cancelled(old, "Escape");

      const previousRoom = await roomId();
      const before = (await room()).turns.map((turn) => turn.id);
      await page.reload();
      await idle();
      assert.equal(await roomId(), previousRoom);
      assert.deepEqual((await room()).turns.map((turn) => turn.id), before);
      await enableInput();
      const recalled = await collect(13, await say(13), "after-reload");
      report.reconnect.push({kind: "reload", retained: before.length, recall: /小林/u.test(recalled.answer)});
      console.log(JSON.stringify(report.reconnect.at(-1)));
    }

    const activeRoom = await roomId();
    const lost = await say(14);
    const beforeLoss = await job(lost);
    await context.setOffline(true);
    const lostNetwork = await page.evaluate(() => fetch("/api/status").then(() => false, () => true));
    assert.equal(lostNetwork, true);
    await delay(350);
    await context.setOffline(false);
    await page.reload();
    await idle();
    const recovered = await job(lost);
    const stale = await page.evaluate(() => livePlayback?.pcmPlaying === true || !audio.paused);
    assert.equal(await roomId(), activeRoom);
    await enableInput();
    const next = await collect(15, await say(15), "after-network-loss");
    report.reconnect.push({kind: "offline-reload", beforeLoss: beforeLoss.phase,
      priorTurn: recovered.phase, stalePlaybackAfterIdle: stale, nextTurn: next.id});
    console.log(JSON.stringify(report.reconnect.at(-1)));
    assert.equal(stale, false, "Reconnection autoplayed an earlier response.");
    assert.deepEqual(report.errors, []);
  } catch (error) {
    report.failure = error.stack;
    throw error;
  } finally {
    await browser?.close();
    if (gpu.exitCode === null && gpu.signalCode === null && gpu.pid) {
      const ended = once(gpu, "exit");
      gpu.kill();
      await ended;
    }
    writeFileSync(join(output, "results.json"), JSON.stringify(report, null, 2));
    console.log(`Report: ${join(output, "results.json")}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
