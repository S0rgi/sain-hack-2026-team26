const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(path.join(__dirname, "../app/static/room.js"), "utf8");
const testKey = "voice-test-access-key";

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function browser(fetchVoice) {
  const nodes = new Map();
  const played = [];
  const paused = [];
  const revoked = [];

  function element(id) {
    if (!nodes.has(id)) {
      const handlers = new Map();
      nodes.set(id, {
        children: [],
        dataset: {},
        value: "",
        addEventListener: (name, handler) => handlers.set(name, handler),
        showModal() { this.open = true; },
        close(value) {
          this.open = false;
          this.returnValue = value;
          handlers.get("close")({ target: this });
        },
      });
    }
    return nodes.get(id);
  }

  const context = vm.createContext({
    document: { getElementById: element, querySelectorAll: () => [] },
    fetch: (url, options) => url.startsWith("/api/voice/")
      ? fetchVoice(url, options)
      : Promise.resolve({ ok: false, status: 503, json: async () => ({}) }),
    AbortController,
    URL: {
      createObjectURL: blob => "blob:" + blob,
      revokeObjectURL: url => revoked.push(url),
    },
    Audio: class {
      constructor(url) { this.url = url; }
      addEventListener() {}
      async play() { played.push(this.url); }
      pause() { paused.push(this.url); }
    },
  });
  vm.runInContext(script + "\nthis.voice = { withVoiceAccess, voiceRequest, playReply, stopPlayback };", context);

  const access = element("voice-access-dialog");
  const input = element("voice-access-key");
  const unlock = (action = () => {}) => {
    context.voice.withVoiceAccess(action);
    input.value = testKey;
    access.close("unlock");
  };
  return { ...context.voice, access, input, unlock, played, paused, revoked };
}

const audioReply = value => ({ ok: true, status: 200, blob: async () => value });

test("the latest clip wins when requests finish out of order", async () => {
  const requests = [];
  const screen = browser((_, options) => {
    const incoming = deferred();
    requests.push({ ...incoming, options });
    return incoming.promise;
  });
  screen.unlock();

  const firstButton = {};
  const secondButton = {};
  const first = screen.playReply("First", firstButton);
  const second = screen.playReply("Second", secondButton);
  assert.equal(requests[0].options.signal.aborted, true);
  requests[1].resolve(audioReply("second"));
  await second;
  requests[0].resolve(audioReply("first"));
  await first;

  assert.deepEqual(screen.played, ["blob:second"]);
  assert.equal(firstButton.disabled, false);
  assert.equal(secondButton.disabled, false);
});

test("starting a new clip pauses the active one and ignores an old body in flight", async () => {
  const readingBody = deferred();
  const pendingBody = deferred();
  const requests = [
    audioReply("playing"),
    { ok: true, status: 200, blob: () => {
      readingBody.resolve();
      return pendingBody.promise;
    } },
    audioReply("latest"),
  ];
  const screen = browser(async () => requests.shift());
  screen.unlock();

  await screen.playReply("Playing", {});
  const obsolete = screen.playReply("Obsolete", {});
  await readingBody.promise;
  await screen.playReply("Latest", {});
  pendingBody.resolve("obsolete");
  await obsolete;

  assert.deepEqual(screen.played, ["blob:playing", "blob:latest"]);
  assert.deepEqual(screen.paused, ["blob:playing"]);
  assert.deepEqual(screen.revoked, ["blob:playing"]);
  screen.stopPlayback();
  assert.deepEqual(screen.paused, ["blob:playing", "blob:latest"]);
  assert.deepEqual(screen.revoked, ["blob:playing", "blob:latest"]);
});

test("voice requests carry the access key and a 401 prompts again", async () => {
  const requests = [];
  const screen = browser(async (url, options) => {
    requests.push({ url, options });
    return url.endsWith("transcribe") ? { status: 401 } : audioReply("recording");
  });

  let continued = 0;
  screen.withVoiceAccess(() => { continued++; });
  assert.equal(screen.access.open, true);
  assert.equal(continued, 0);
  screen.access.close("cancel");
  assert.equal(continued, 0);

  screen.unlock(() => { continued++; });
  assert.equal(continued, 1);
  assert.equal(screen.input.value, "");
  screen.withVoiceAccess(() => { continued++; });
  assert.equal(continued, 2);
  assert.equal(screen.access.open, false);

  await screen.playReply("Read this", {});
  await assert.rejects(screen.voiceRequest("/api/voice/transcribe", {
    method: "POST", body: "audio",
  }), /Ключ доступа к голосу отклонён/);
  assert.deepEqual(requests.map(item => item.url), ["/api/voice/speech", "/api/voice/transcribe"]);
  for (const { options } of requests) {
    assert.equal(options.headers["X-Voice-Access-Key"], testKey);
    assert.equal(options.mode, "same-origin");
    assert.equal(options.redirect, "error");
  }

  screen.withVoiceAccess(() => { continued++; });
  assert.equal(screen.access.open, true);
  assert.equal(continued, 2);
});
