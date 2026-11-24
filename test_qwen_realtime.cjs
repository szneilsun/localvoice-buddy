"use strict";

const assert = require("node:assert/strict");
const {chromium} = require("playwright");

async function main() {
  const microphoneWav = process.env.VOICE_TEST_MIC_WAV;
  const browser = await chromium.launch({
    executablePath: process.env.EDGE_PATH, headless: true,
    args: microphoneWav ? [
      "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
      `--use-file-for-fake-audio-capture=${microphoneWav}%noloop`,
    ] : [],
  });
  try {
    const url = process.env.VOICE_URL || "http://127.0.0.1:8010";
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(url);
    await page.locator("#tts-quality + span").click();

    async function submit(ttsMode) {
      const submitted = page.waitForResponse((response) =>
        response.url().endsWith("/api/sample") && response.status() === 202);
      await page.locator("#sample").click();
      const {id, pcm} = await (await submitted).json();
      assert.equal(pcm, ttsMode === "quality");
      return id;
    }

    async function job(id) {
      return page.evaluate(async (token) => (await fetch(`/api/turn/${token}`, {
        headers: {"X-Conversation-Id": sessionStorage.getItem("voice-conversation")},
      })).json(), id);
    }

    const interrupted = await submit("quality");
    await page.locator("#first-audio").filter({hasText: "首段开始播放"}).waitFor();
    assert.equal(await page.evaluate(() => livePlayback.pcmPlaying), true);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => livePlayback === null);
    await page.waitForFunction(async (id) => {
      const response = await fetch(`/api/turn/${id}`, {
        headers: {"X-Conversation-Id": sessionStorage.getItem("voice-conversation")},
      });
      return (await response.json()).phase === "cancelled";
    }, interrupted);

    const firstBlocks = [];
    for (let i = 0; i < 3; i++) {
      const stream = page.waitForResponse((response) => response.url().endsWith("/stream.pcm"));
      const id = await submit("quality");
      const response = await stream;
      assert.equal(response.status(), 200);
      assert.equal(response.headers()["transfer-encoding"], "chunked");
      await page.locator("#first-audio").filter({hasText: "首段开始播放"}).waitFor();
      const firstPlayed = await page.locator("#first-audio").textContent();
      await page.locator("#sample").waitFor();
      await page.waitForFunction(() => !document.getElementById("sample").disabled);
      const completed = await job(id);
      assert.equal(completed.phase, "done");
      assert.equal(completed.tts_backend, "qwen-graph");
      assert.equal((await response.body()).length, completed.pcm_bytes);
      const stats = await page.evaluate(() => ({
        received: livePlayback.pcmReceived, underruns: livePlayback.underruns || 0,
      }));
      assert.equal(stats.received, completed.pcm_bytes);
      assert.match(await page.locator("#audio").getAttribute("src"), /reply\.wav/);
      firstBlocks.push({pcm_s: completed.first_audio, playback: firstPlayed, ...stats});
    }
    await page.locator("#tts-fast + span").click();
    const fast = await submit("fast");
    await page.waitForFunction(() => !document.getElementById("sample").disabled);
    assert.equal((await job(fast)).phase, "done");
    let mic;
    if (microphoneWav) {
      await page.locator("#tts-quality + span").click();
      const upload = page.waitForResponse((response) =>
        response.url().endsWith("/api/turn") && response.status() === 202);
      await page.locator("label:has(#voice-toggle)").click();
      const {id} = await (await upload).json();
      await page.locator("#first-audio").filter({hasText: "首段开始播放"}).waitFor();
      await page.waitForFunction(() => !document.getElementById("sample").disabled);
      const completed = await job(id);
      assert.equal(completed.phase, "done");
      assert.equal(completed.tts_backend, "qwen-graph");
      mic = {transcript: completed.transcript, pcm_s: completed.first_audio,
        playback: await page.locator("#first-audio").textContent()};
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({quality: firstBlocks, escape: "cancelled", piper: "done", mic}, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
