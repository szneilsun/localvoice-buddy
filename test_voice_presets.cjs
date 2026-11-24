"use strict";

const assert = require("node:assert/strict");
const {chromium} = require("playwright");

async function main() {
  const browser = await chromium.launch({executablePath: process.env.EDGE_PATH});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(process.env.VOICE_URL || "http://127.0.0.1:8010");
    const select = page.locator("#tts-speaker");
    const preview = page.locator("#voice-preview");
    const idle = () => page.waitForFunction(() => mode === "idle");
    const read = (path, room) => page.evaluate(async ({path, room}) => {
      const response = await fetch(path, {
        headers: {"X-Conversation-Id": room || sessionStorage.getItem("voice-conversation")},
      });
      return response.json();
    }, {path, room});
    const voices = ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric"];
    await page.locator("#tts-quality + span").click();
    await select.waitFor();
    assert.deepEqual(await select.locator("option").evaluateAll((options) => options.map((o) => o.value)), voices);
    const chart = [];
    for (const voice of voices) {
      await select.selectOption(voice);
      const posted = page.waitForResponse((response) =>
        response.url().endsWith("/api/voice-preview") && response.request().method() === "POST");
      await preview.click();
      const response = await posted;
      assert.equal(response.status(), 202, await response.text());
      assert.equal(response.request().headers()["x-voice-speaker"], voice);
      const {id, room} = await response.json();
      await page.waitForFunction(() => operation?.previewState?.startedPlayback);
      assert.equal(await preview.getAttribute("aria-pressed"), "true");
      await idle();
      const job = await read(`/api/turn/${id}`, room);
      assert.equal(job.phase, "done");
      assert.equal(job.speaker, voice);
      assert.equal(job.tts_backend, "qwen-graph");
      assert.ok(job.pcm_bytes > 0);
      chart.push({voice, firstPcm_s: job.first_audio});
    }
    assert.deepEqual(await read("/api/conversation"), {turns: [], latest: null});

    await select.selectOption("Uncle_Fu");
    const pending = page.waitForResponse((response) =>
      response.url().endsWith("/api/voice-preview") && response.status() === 202);
    await preview.click();
    const stopped = await (await pending).json();
    await page.waitForFunction(() => operation?.previewState?.startedPlayback);
    const cancelled = page.waitForResponse((response) =>
      response.url().endsWith(`/api/turn/${stopped.id}/cancel`) && response.status() === 200);
    await page.keyboard.press("Escape");
    await cancelled;
    await idle();
    assert.equal((await read(`/api/turn/${stopped.id}`, stopped.room)).phase, "cancelled");

    await page.reload();
    await select.waitFor();
    assert.equal(await select.inputValue(), "Uncle_Fu");
    await page.setViewportSize({width: 320, height: 800});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.setViewportSize({width: 1200, height: 850});
    const sampled = page.waitForResponse((response) =>
      response.url().endsWith("/api/sample") && response.status() === 202);
    await page.locator("#sample").click();
    const response = await sampled;
    assert.equal(response.request().headers()["x-voice-speaker"], "Uncle_Fu");
    const {id} = await response.json();
    await idle();
    const turn = await read(`/api/turn/${id}`);
    assert.equal(turn.phase, "done");
    assert.equal(turn.tts_backend, "qwen-graph");
    assert.equal(turn.speaker, "Uncle_Fu");
    assert.ok(await page.evaluate(() => livePlayback.startedPlayback));
    await page.locator("#tts-fast + span").click();
    assert.equal(await select.isVisible(), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({previews: chart, escaped: true, persisted: true, reply: turn.speaker}));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
