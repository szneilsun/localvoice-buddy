import assert from "node:assert/strict";
import {test} from "node:test";
import {VoiceSegments} from "./voice-input.mjs";

const frame = (value) => new Float32Array(512).fill(value);

test("keeps the start of speech and stops after half a second of quiet", () => {
  let starts = 0;
  const clips = [];
  const gate = new VoiceSegments({
    start: () => { starts++; return true; },
    end: (samples) => clips.push(samples),
  });
  for (let i = 0; i < 10; i++) gate.push(0, frame(0.125));
  for (const score of [0.9, 0.25, 0.9, 0.48]) gate.push(score, frame(0.5));
  assert.equal(starts, 0);
  gate.push(0.9, frame(0.5));
  assert.equal(starts, 1);
  for (let i = 0; i < 15; i++) gate.push(0, frame(0));
  assert.equal(clips.length, 0);
  gate.push(0, frame(0));
  assert.equal(clips.length, 1);
  assert.equal(clips[0].length, 19 * 512);
  assert.equal(clips[0][0], 0.125);
  assert.equal(clips[0][10 * 512], 0.5);
});

test("keeps a soft first syllable through a longer delayed VAD confirmation", () => {
  for (const playing of [false, true]) {
    const clips = [];
    const gate = new VoiceSegments({
      start: () => true, end: (samples) => clips.push(samples), playing: () => playing,
    });
    for (let i = 0; i < 30; i++) gate.push(0, frame(0));
    gate.push(0.18, frame(0.0625));
    for (let i = 0; i < 21; i++) gate.push(0.48, frame(0.125));
    for (let i = 0; i < 5; i++) gate.push(0.9, frame(0.5));
    for (let i = 0; i < 16; i++) gate.push(0, frame(0));
    assert.equal(clips.length, 1);
    const onset = clips[0].findIndex((sample) => sample !== 0);
    assert.ok(onset > 0);
    assert.equal(clips[0][onset], 0.0625);
    assert.equal(clips[0][onset + 22 * 512], 0.5);
  }
});

test("does not reuse idle speech evidence when playback begins", () => {
  let playing = false;
  let starts = 0;
  const gate = new VoiceSegments({
    start: () => { starts++; return true; }, end: () => {}, playing: () => playing,
  });
  gate.push(0.9, frame(0.1));
  gate.push(0.9, frame(0.1));
  playing = true;
  for (const score of [0.9, 0.9, 0.65, 0.9, 0.9, 0.9, 0.9]) gate.push(score, frame(0.1));
  assert.equal(starts, 0);
  gate.push(0.9, frame(0.1));
  assert.equal(starts, 1);
});

test("requires stronger evidence during playback and ignores rejected speech until silence", () => {
  let starts = 0;
  let canRecord = false;
  const gate = new VoiceSegments({
    start: () => { starts++; return canRecord; },
    end: () => {},
    playing: () => true,
  });
  for (let i = 0; i < 20; i++) gate.push(0.65, frame(0.1));
  assert.equal(starts, 0);
  for (let i = 0; i < 5; i++) gate.push(0.9, frame(0.1));
  assert.equal(starts, 1);
  canRecord = true;
  for (let i = 0; i < 10; i++) gate.push(0.9, frame(0.1));
  assert.equal(starts, 1);
  for (let i = 0; i < 16; i++) gate.push(0, frame(0));
  for (let i = 0; i < 5; i++) gate.push(0.9, frame(0.1));
  assert.equal(starts, 2);
});

test("caps continuous speech below 15 seconds without duplicating a turn", () => {
  const clips = [];
  const voiced = frame(0.5);
  const gate = new VoiceSegments({start: () => true, end: (samples) => clips.push(samples)});
  for (let i = 0; i < 600; i++) gate.push(0.9, voiced);
  assert.equal(clips.length, 1);
  assert.equal(clips[0].length, 468 * 512);
});
