"use strict";

const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");

const app = readFileSync(`${__dirname}/app.js`, "utf8");
const origin = "http://127.0.0.1:8000";
const first = "a".repeat(32);
const second = "b".repeat(32);

function harness() {
  const nodes = new Map();
  const events = {};
  const requests = [];
  let plays = 0;
  const document = {
    querySelectorAll: () => [],
    createElement: () => ({}),
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {
        dataset: {}, classList: {toggle() {}},
        paused: true, hidden: true, ended: false, value: "",
        setAttribute() {}, removeAttribute() { this.src = ""; },
        getContext: () => null, querySelectorAll: () => [], replaceChildren() {},
        showModal() { this.open = true; },
        close() { this.open = false; this.onclose?.(); },
        focus() { document.activeElement = this; },
        load() {}, pause() { this.paused = true; },
        play() { this.paused = false; plays++; return Promise.resolve(); },
        addEventListener(name, callback) { events[`audio:${name}`] = callback; },
      });
      return nodes.get(id);
    },
  };
  class Element {}
  const context = vm.createContext({
    document, Element, URL, Headers, AbortSignal, AbortController, setTimeout, performance,
    ResizeObserver: class { observe() {} }, setInterval() {}, location: {href: origin},
    sessionStorage: {getItem: () => "c".repeat(32), setItem() {}},
    window: {MediaRecorder: true, addEventListener: (name, callback) => { events[name] = callback; }},
    navigator: {mediaDevices: {getUserMedia: () => new Promise(() => {}), addEventListener() {}}},
    fetch(path, init) {
      return new Promise((resolve) => {
        requests.push({path, init, reply: (data) => resolve({ok: true, json: async () => data})});
      });
    },
  });
  vm.runInContext(app, context);

  async function next(path) {
    for (let i = 0; i < 200; i++) {
      const index = requests.findIndex((request) => request.path === path);
      if (index >= 0) return requests.splice(index, 1)[0];
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.fail(`Missing request: ${path}`);
  }
  const el = (id) => document.getElementById(id);
  const mode = () => vm.runInContext("mode", context);
  return {el, events, requests, next, mode, plays: () => plays, document};
}

async function openPage(page, status = {llama: true}) {
  (await page.next("/api/status")).reply(status);
  (await page.next("/api/conversation")).reply({turns: [], latest: null});
  await new Promise(setImmediate);
  assert.equal(page.mode(), "idle");
}

test("the connection guide shows the advertised LAN address and public certificate", async () => {
  const page = harness();
  await openPage(page);
  page.el("access-guide").open = true;
  const update = page.el("access-guide").ontoggle();
  (await page.next("/api/access")).reply({
    local_url: "http://127.0.0.1:8010/", model_port: 8001,
    lan: {url: "https://192.168.1.20:8443/", ready: true, certificate: true},
  });
  await update;
  assert.equal(page.el("local-url").textContent, "http://127.0.0.1:8010/");
  assert.equal(page.el("lan-url").href, "https://192.168.1.20:8443/");
  assert.equal(page.el("lan-status").textContent.trim(), "· HTTPS 已启动");
  assert.equal(page.el("lan-certificate").hidden, false);
});

test("the reply model dialog saves remotely without retaining the key in the form", async () => {
  const page = harness();
  await openPage(page, {llama: false, chat_provider: "local"});
  const opening = page.el("chat-settings").onclick();
  (await page.next("/api/chat-settings")).reply({provider: "local", url: "", model: "", has_api_key: false});
  await opening;
  assert.equal(page.el("chat-remote-fields").hidden, true);
  page.el("chat-remote").checked = true;
  page.el("chat-remote").onchange();
  assert.equal(page.el("chat-remote-fields").hidden, false);
  assert.equal(page.el("chat-key").required, true);
  page.el("chat-url").value = "https://chat.example.test/v1";
  page.el("chat-model").value = "chat";
  page.el("chat-key").value = "dummy-key";

  const saving = page.el("chat-form").onsubmit({preventDefault() {}});
  const save = await page.next("/api/chat-settings");
  assert.deepEqual(JSON.parse(save.init.body), {
    provider: "remote", url: "https://chat.example.test/v1", model: "chat", api_key: "dummy-key",
  });
  const saved = {provider: "remote", url: "https://chat.example.test/v1", model: "chat", has_api_key: true};
  save.reply(saved);
  await saving;
  assert.equal(page.el("chat-dialog").open, false);
  assert.equal(page.el("processing-location").textContent, "文本发往远程");
  assert.equal(page.el("chat-key").value, "");

  (await page.next("/api/status")).reply({llama: false, chat_provider: "remote"});
  const reopening = page.el("chat-settings").onclick();
  (await page.next("/api/chat-settings")).reply(saved);
  await reopening;
  assert.equal(page.el("chat-key").required, false);
  assert.equal(page.el("chat-key").value, "");
  page.el("chat-close").onclick();
  page.el("sample").onclick();
  assert.equal((await page.next("/api/sample")).init.headers.get("X-Voice-Chat-Provider"), "remote");
});

test("the fast voice is selected by default and the quality choice is sent with a turn", async () => {
  const page = harness();
  await openPage(page, {llama: true, tts_modes: ["fast", "quality"]});
  assert.equal(page.el("tts-setting").hidden, false);
  assert.equal(page.el("tts-fast").checked, true);
  page.el("tts-quality").checked = true;
  page.el("tts-quality").onchange();
  page.el("sample").onclick();
  assert.equal((await page.next("/api/sample")).init.headers.get("X-Voice-TTS"), "quality");
});

test("the selected preset applies to replies and an early stop cancels only the preview", async () => {
  const page = harness();
  await openPage(page, {llama: true, tts_modes: ["fast", "quality"], quality_pcm: true,
    quality_voices: {Vivian: "Vivian", Serena: "Serena"}});
  page.el("tts-quality").checked = true;
  page.el("tts-quality").onchange();
  (await page.next("/api/warm-quality")).reply({warming: false});
  page.el("tts-speaker").value = "Serena";
  page.el("tts-speaker").onchange();
  page.el("answer").textContent = "Previous reply";
  page.el("voice-preview").onclick();
  const preview = await page.next("/api/voice-preview");
  assert.equal(preview.init.headers.get("X-Voice-Speaker"), "Serena");
  page.el("voice-preview").onclick();
  preview.reply({id: first, room: second, pcm: true});
  assert.equal(page.el("answer").textContent, "Previous reply");
  const cancel = await page.next(`/api/turn/${first}/cancel`);
  assert.equal(cancel.init.headers.get("X-Conversation-Id"), second);
  cancel.reply({phase: "cancelled"});
  page.el("sample").onclick();
  const turn = await page.next("/api/sample");
  assert.equal(turn.init.headers.get("X-Voice-Speaker"), "Serena");
  assert.equal(turn.init.headers.get("X-Conversation-Id"), "c".repeat(32));
});

test("interrupt before POST returns cancels its ID before submitting another turn", async () => {
  const page = harness();
  await openPage(page);
  page.el("sample").onclick();
  const slowPost = await page.next("/api/sample");
  assert.equal(page.el("interrupt").disabled, false);
  page.el("interrupt").onclick();
  assert.equal(page.mode(), "idle");
  assert.equal(page.el("record").disabled, false);
  assert.equal(page.document.activeElement, page.el("record"));
  page.el("sample").onclick();
  slowPost.reply({id: first});
  const cancel = await page.next(`/api/turn/${first}/cancel`);
  assert.equal(cancel.init.method, "POST");
  assert.equal(cancel.init.headers.get("X-Conversation-Id"), "c".repeat(32));
  assert.equal(page.requests.length, 0);
  cancel.reply({phase: "cancelled"});
  (await page.next("/api/sample")).reply({id: second});
  await page.next("/api/conversation");
  assert.equal(page.mode(), "processing");
  assert.equal(page.plays(), 0);
});

test("Escape stops audio immediately and late polling cannot replace a new mic state", async () => {
  const page = harness();
  await openPage(page);
  page.el("sample").onclick();
  (await page.next("/api/sample")).reply({id: first});
  (await page.next("/api/conversation")).reply({turns: []});
  const url = `/api/turn/${first}`;
  (await page.next(url)).reply({id: first, phase: "chat", parts: 1, answer: "Old reply"});
  const stale = await page.next(url);
  assert.equal(page.plays(), 1);
  assert.equal(page.el("audio").paused, false);
  let prevented = false;
  page.events.keydown({key: "Escape", preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(page.el("audio").paused, true);
  assert.equal(page.el("audio").src, "");
  assert.equal(stale.init.signal.aborted, true);
  assert.equal(page.el("turn-status").textContent, "已打断");
  page.events.keydown({code: "Space", target: page.el("record"), preventDefault() {}});
  assert.equal(page.mode(), "permission");
  stale.reply({id: first, phase: "done", parts: 2, answer: "Late reply"});
  (await page.next(`${url}/cancel`)).reply({phase: "cancelled"});
  await new Promise(setImmediate);
  assert.equal(page.mode(), "permission");
  assert.equal(page.el("audio").hidden, true);
  assert.equal(page.plays(), 1);
});

test("plays streamed parts in order before the completed reply is available", async () => {
  const page = harness();
  await openPage(page);
  page.el("sample").onclick();
  (await page.next("/api/sample")).reply({id: first});
  (await page.next("/api/conversation")).reply({turns: []});
  const url = `/api/turn/${first}`;
  const audio = page.el("audio");
  (await page.next(url)).reply({id: first, phase: "chat", parts: 1, answer: "First sentence."});
  const next = await page.next(url);
  assert.match(audio.src, /part-0\.wav/);
  assert.equal(page.el("answer").textContent, "First sentence.");
  assert.equal(page.el("download").hidden, true);
  audio.ended = true;
  page.events["audio:ended"]();
  assert.equal(page.plays(), 1);
  next.reply({id: first, phase: "chat", parts: 3, answer: "First sentence. More text."});
  const done = await page.next(url);
  assert.match(audio.src, /part-1\.wav/);
  assert.equal(page.plays(), 2);
  page.events["audio:ended"]();
  assert.match(audio.src, /part-2\.wav/);
  done.reply({id: first, phase: "done", parts: 3, answer: "Full reply.", seconds: 4, timings: {}});
  (await page.next("/api/conversation")).reply({turns: []});
  assert.match(audio.src, /part-2\.wav/);
  page.events["audio:ended"]();
  assert.match(audio.src, /reply\.wav/);
  assert.equal(page.plays(), 3);
  assert.equal(page.el("download").hidden, false);
});

test("refreshing during a reply restores the result without autoplaying it", async () => {
  const page = harness();
  (await page.next("/api/status")).reply({llama: true, tts_modes: ["fast", "quality"]});
  const job = {id: first, phase: "tts", pcm: true, parts: 0, answer: "Before reload"};
  (await page.next("/api/conversation")).reply({turns: [], latest: job});
  const complete = {...job, phase: "done", seconds: 4, timings: {}};
  (await page.next(`/api/turn/${first}`)).reply(complete);
  (await page.next("/api/conversation")).reply({turns: [complete], latest: complete});
  await new Promise(setImmediate);
  assert.equal(page.mode(), "idle");
  assert.equal(page.plays(), 0);
  assert.equal(page.el("audio").paused, true);
  assert.match(page.el("audio").src, /reply\.wav/);
  assert.equal(page.el("download").hidden, false);
});
