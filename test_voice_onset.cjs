"use strict";

const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {chromium} = require("playwright");
const {setTimeout: delay} = require("node:timers/promises");

function pcmSamples(wav) {
  for (let offset = 12; offset + 8 <= wav.length;) {
    const bytes = wav.readUInt32LE(offset + 4);
    if (wav.toString("ascii", offset, offset + 4) === "data") {
      return Int16Array.from({length: bytes / 2}, (_, i) => wav.readInt16LE(offset + 8 + 2 * i));
    }
    offset += 8 + bytes + (bytes % 2);
  }
  throw new Error("Missing WAV data chunk.");
}

function sourceOffset(source, uploaded) {
  const anchor = 16000;
  const segment = source.subarray(anchor, anchor + 2048);
  const energy = segment.reduce((sum, sample) => sum + sample ** 2, 0);
  let score = -Infinity;
  let lag = 0;
  for (let i = 9000; i < Math.min(23000, uploaded.length - segment.length); i++) {
    let dot = 0;
    let capturedEnergy = 0;
    for (let j = 0; j < segment.length; j++) {
      dot += segment[j] * uploaded[i + j];
      capturedEnergy += uploaded[i + j] ** 2;
    }
    const similarity = dot / Math.sqrt(energy * capturedEnergy);
    if (similarity > score) {
      score = similarity;
      lag = i;
    }
  }
  assert.ok(score > 0.6, `Could not align recorded speech to reference: ${score}`);
  return (anchor - lag) / 16000;
}

async function main() {
  const clip = process.env.VOICE_TEST_MIC_WAV;
  const cold = process.argv.includes("--cold");
  assert.ok(clip, "Set VOICE_TEST_MIC_WAV to checks/shanghai-question.wav");
  const browser = await chromium.launch({
    executablePath: process.env.EDGE_PATH,
    args: [
      "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
      `--use-file-for-fake-audio-capture=${clip}%noloop`,
    ],
  });
  let releaseModel;
  const holdModel = new Promise((resolve) => { releaseModel = resolve; });
  try {
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    if (cold) {
      await page.route("**/vad/silero_vad_v5.onnx", async (route) => {
        await holdModel;
        await route.continue();
      });
    }
    if (cold) {
      await page.route("**/api/turn", (route) =>
        route.fulfill({status: 409, json: {error: "Capture-only test"}}));
    }
    await page.goto(process.env.VOICE_URL || "http://127.0.0.1:8010", {waitUntil: "domcontentloaded"});
    await page.locator("#tts-fast + span").click();
    const upload = page.waitForResponse((response) =>
      response.url().endsWith("/api/turn") && response.request().method() === "POST");
    await page.locator("label:has(#voice-toggle)").click();
    await page.locator("#input-state").filter({hasText: "自动监听中"}).waitFor();
    if (cold) await delay(900);
    releaseModel();
    const response = await upload;
    if (cold) {
      const bySeconds = sourceOffset(pcmSamples(readFileSync(clip)), pcmSamples(response.request().postDataBuffer()));
      assert.ok(bySeconds <= 0.08, `Uploaded WAV lost ${bySeconds} seconds of the fixture`);
      assert.deepEqual(pageErrors, []);
      console.log(JSON.stringify({delayedVadMs: 900, uploadedSourceOffsetMs: Math.round(bySeconds * 1000)}));
      return;
    }
    assert.equal(response.status(), 202);
    const {id} = await response.json();
    await page.waitForFunction(async (token) => {
      const response = await fetch(`/api/turn/${token}`, {
        headers: {"X-Conversation-Id": sessionStorage.getItem("voice-conversation")},
      });
      return (await response.json()).phase === "done";
    }, id, {polling: 250});
    await page.waitForFunction(() => !document.getElementById("sample").disabled);
    const transcript = await page.locator("#transcript").textContent();
    assert.match(transcript, /^请用/u);
    assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({id, transcript}));
  } finally {
    releaseModel();
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
