"use strict";

const assert = require("node:assert/strict");
const {createHash, X509Certificate} = require("node:crypto");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {chromium} = require("playwright");

async function main() {
  const directory = join(__dirname, ".voice-lan");
  const config = JSON.parse(readFileSync(join(directory, "access.json"), "utf8"));
  const certificate = new X509Certificate(readFileSync(join(directory, "server.pem")));
  const publicKey = certificate.publicKey.export({type: "spki", format: "der"});
  const pin = createHash("sha256").update(publicKey).digest("base64");
  const url = `https://${config.lanIp}:${config.port}`;
  const microphone = process.env.VOICE_TEST_MIC_WAV;
  assert.ok(microphone, "Set VOICE_TEST_MIC_WAV to checks/shanghai-question.wav.");
  const browser = await chromium.launch({
    executablePath: process.env.EDGE_PATH,
    args: [
      `--ignore-certificate-errors-spki-list=${pin}`,
      "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
      `--use-file-for-fake-audio-capture=${microphone}%noloop`,
    ],
  });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const response = await page.goto(url);
    assert.equal(response.status(), 200);
    assert.equal(response.headers()["www-authenticate"], undefined);
    assert.equal(await page.locator("#chat-settings").isHidden(), true);
    assert.equal(await page.evaluate(() => isSecureContext && !!navigator.mediaDevices?.getUserMedia), true);
    await page.locator("#tts-quality + span").click();
    const upload = page.waitForResponse((response) =>
      response.url() === `${url}/api/turn` && response.request().method() === "POST");
    const streamed = page.waitForResponse((response) => response.url().endsWith("/stream.pcm"));
    await page.locator("label:has(#voice-toggle)").click();
    assert.equal((await upload).status(), 202);
    const stream = await streamed;
    assert.equal(stream.status(), 200);
    assert.equal(stream.headers()["transfer-encoding"], "chunked");
    await page.locator("#first-audio").filter({hasText: "首段开始播放"}).waitFor();
    await page.waitForFunction(() => mode === "idle");
    const playback = await page.evaluate(() => ({
      received: livePlayback.pcmReceived, expected: livePlayback.pcmExpected,
      failed: !!livePlayback.pcmFailed, transcript: document.getElementById("transcript").textContent,
    }));
    assert.equal(playback.failed, false);
    assert.ok(playback.received > 0);
    assert.equal(playback.received, playback.expected);
    assert.match(playback.transcript, /^请用/u);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({secureContext: true, chunkedPcm: playback.received, speech: playback.transcript}));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
