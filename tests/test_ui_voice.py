"""Voice mode JavaScript in static/ui.html, exercised under node.

The recorder state machine, the transcript hand-off and the autoplay fallback
are the parts that cannot be checked by reading the Python, and the two that
have actually broken in browsers are autoplay refusal and a double-tap on the
mic. There is no build step and no JS test runner in the repo, so this drives
the page's own functions against a minimal DOM.
"""

import re
import shutil
import subprocess

import pytest

UI = "src/chatbot/static/ui.html"

# Stable per-id elements: unlike a factory that returns a fresh node per
# getElementById, the voice code writes to #input and reads #mic back, so the
# same object has to come back each time.
HARNESS = r"""
const assert = require("assert");

function el(tag) {
  const classes = new Set();
  return {
    tag, className: "", textContent: "", href: "", title: "", value: "",
    innerHTML: "", disabled: false, children: [], attrs: {},
    style: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      contains: (c) => classes.has(c),
      _set: classes,
    },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    querySelector() { return null; },
    addEventListener() {},
    remove() {},
    focus() { this.focused = true; },
    setSelectionRange() {},
  };
}

const byId = {};
global.document = {
  createElement: el,
  getElementById(id) { return byId[id] || (byId[id] = el("div")); },
  querySelectorAll: () => [],
  addEventListener() {},
};
global.location = { search: "", hash: "", href: "", assign() {} };
global.window = { location: global.location, isSecureContext: true };
global.localStorage = {
  store: {}, getItem(k) { return this.store[k] ?? null; },
  setItem(k, v) { this.store[k] = v; }, removeItem(k) { delete this.store[k]; },
};
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.alert = () => {};
global.EventSource = class {};

// The page's own init() must not race the assertions.
global.fetch = () => new Promise(() => {});

// --- voice-specific stubs, reassigned per test -------------------------------
// node >= 21 defines a read-only global `navigator`, so plain assignment is a
// silent no-op; defineProperty is the only way to install a fake.
function setNavigator(value) {
  Object.defineProperty(globalThis, "navigator", {
    value, configurable: true, writable: true,
  });
}
setNavigator({ mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) } });
global.MediaRecorder = undefined;
global.AudioContext = undefined;
global.URL.createObjectURL = () => "blob:fake";
global.URL.revokeObjectURL = () => {};

global.played = [];
global.Audio = class {
  constructor(src) { this.src = src; this.currentTime = 0; }
  play() { global.played.push(this.src); return global.playRejects
    ? Promise.reject(new DOMExceptionish("blocked")) : Promise.resolve(); }
  pause() { this.paused = true; }
};
class DOMExceptionish extends Error {}
"""

ASSERTIONS = r"""
// Wrapped because the recorder and playback paths are async, and mixing a
// top-level await with require() makes node guess the module format.
(async () => {

function assertIncludes(haystack, needle, what) {
  assert.ok(String(haystack).includes(needle), what + ": got " + JSON.stringify(haystack));
}

// --- speechText: citations must not be read aloud ---------------------------
assert.strictEqual(speechText("Jupiter has many moons."), "Jupiter has many moons.",
  "plain prose is untouched");
assertIncludes(speechText("See [p. 5-7] for detail."), "See", "bracket citation dropped");
assert.ok(!speechText("See [p. 5-7] for detail.").includes("5-7"),
  "page numbers are not spoken");
assert.ok(!speechText("Revenue rose (p. 12).").includes("12"), "paren page ref dropped");
assert.ok(!speechText("Revenue rose (pages 12-14).").includes("12"), "pages paren dropped");
assert.strictEqual(speechText("  spaced   out \n text "), "spaced out text", "whitespace");
assert.strictEqual(speechText(""), "", "empty stays empty");
assert.strictEqual(speechText(null), "", "null is safe");

// --- extension mapping: the filename must match what the browser recorded ---
assert.strictEqual(extForMime("audio/webm;codecs=opus"), "webm", "chrome");
assert.strictEqual(extForMime("audio/mp4"), "mp4", "safari");
assert.strictEqual(extForMime("audio/ogg;codecs=opus"), "ogg", "firefox");
assert.strictEqual(extForMime(""), "webm", "unknown falls back");

// No MediaRecorder means no recording support, and the picker must say so
// rather than returning something unusable.
assert.strictEqual(pickRecorderMime(), "", "no MediaRecorder, no mime");
global.MediaRecorder = class { static isTypeSupported(t) { return t === "audio/mp4"; } };
assert.strictEqual(pickRecorderMime(), "audio/mp4", "first supported wins");
global.MediaRecorder = class { static isTypeSupported() { return true; } };
assert.strictEqual(pickRecorderMime(), "audio/webm;codecs=opus", "prefers opus webm");

// --- the mic button always reflects voice.state ------------------------------
setVoiceState("listening");
assert.strictEqual($("mic").textContent, "■", "listening shows stop");
assert.strictEqual($("mic").classList.contains("recording"), true, "listening styled");
assertIncludes($("mic").title, "Stop", "listening title");

setVoiceState("transcribing");
assert.strictEqual($("mic").disabled, true, "busy while transcribing");
assert.strictEqual($("mic").classList.contains("recording"), false, "not recording style");
assert.strictEqual($("mic").textContent, "🎤", "mic glyph restored");

setVoiceState("idle");
assert.strictEqual($("mic").disabled, false, "re-enabled when idle");
assertIncludes($("mic").title, "Speak", "idle title");

// --- the notice line is how every failure is communicated --------------------
// It must survive the return to idle: clearing it there would erase the
// explanation the moment it was written.
voiceNotice("something went wrong");
setVoiceState("idle");
assertIncludes($("voice-notice").textContent, "something went wrong",
  "returning to idle must not erase the notice");

// --- permission denial must say what to do, not just "error" -----------------
let notAllowed = Object.assign(new Error("denied"), { name: "NotAllowedError" });
setNavigator({ mediaDevices: { getUserMedia: async () => { throw notAllowed; } } });
await startRecording();
assertIncludes($("voice-notice").textContent, "permission denied", "denied explains itself");
assert.strictEqual(voice.state, "idle", "back to idle after denial");

let notFound = Object.assign(new Error("none"), { name: "NotFoundError" });
setNavigator({ mediaDevices: { getUserMedia: async () => { throw notFound; } } });
await startRecording();
assertIncludes($("voice-notice").textContent, "No microphone", "missing mic explains itself");

// No recording API at all: say so and do not throw.
setNavigator({});
await startRecording();
assertIncludes($("voice-notice").textContent, "cannot record", "unsupported browser");
assert.strictEqual(voice.state, "idle", "idle after unsupported");

// --- transcript lands in the composer, unsent -------------------------------
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ text: "IFRS S1 targets?" }) });
$("input").value = "";
await transcribe(new Blob(["audio"], { type: "audio/webm" }));
assert.strictEqual($("input").value, "IFRS S1 targets?", "transcript is editable in the box");
assertIncludes($("voice-notice").textContent, "edit if needed", "user is told to review");
assert.strictEqual(voice.state, "idle", "idle after transcribe");

// Silence produces no query rather than an empty send.
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ text: "   " }) });
$("input").value = "kept";
await transcribe(new Blob(["audio"], { type: "audio/webm" }));
assert.strictEqual($("input").value, "kept", "empty transcript does not clobber the box");
assertIncludes($("voice-notice").textContent, "Didn't catch that", "empty transcript explains");

// A 503 from an unconfigured provider surfaces the server's message.
global.fetch = async () => ({ ok: false, status: 503,
  json: async () => ({ detail: "faster-whisper is not installed" }) });
await transcribe(new Blob(["audio"], { type: "audio/webm" }));
assertIncludes($("voice-notice").textContent, "faster-whisper", "server detail shown");

// --- autoplay refusal falls back to the replay button ------------------------
global.fetch = async () => ({ ok: true, status: 200, blob: async () => new Blob(["mp3"]) });
global.playRejects = true;
const msg = $("messages");
await speak("Hello there.", null);
assertIncludes($("voice-notice").textContent, "Tap", "offers the replay button instead");
assert.strictEqual(voice.state, "idle", "not left stuck speaking");
global.playRejects = false;

// --- and the happy path does play -------------------------------------------
global.played.length = 0;
await speak("Hello there.", null);
assert.strictEqual(global.played.length, 1, "audio played");
assert.strictEqual(voice.state, "speaking", "state is speaking");
assertIncludes(voice.audioUrl, "blob:", "object url in use");
stopSpeaking();
assert.strictEqual(voice.state, "idle", "stop returns to idle");
assert.strictEqual(voice.audio, null, "audio released");

// --- auto-speak toggle is persisted and reflected ----------------------------
const wasAuto = voice.autoSpeak;
toggleAutoSpeak();
assert.strictEqual(voice.autoSpeak, !wasAuto, "toggled");
assert.strictEqual(localStorage.getItem("voice_autospeak"), voice.autoSpeak ? "on" : "off", "persisted");
assert.strictEqual($("voice-auto").classList.contains("off"), !voice.autoSpeak, "styled when muted");
toggleAutoSpeak();
assert.strictEqual(voice.autoSpeak, wasAuto, "toggles back");

// --- the actual interaction: press to record, press again to transcribe ------
// The fake delivers dataavailable/stop on a later tick, exactly as a browser
// does. A synchronous fake would hide the bug where the chunks are cleared
// before the events arrive and every recording comes back empty.
const tick = () => new Promise((r) => setTimeout(r, 0));
let recorded = [];
global.MediaRecorder = class {
  constructor(stream, opts) {
    this.stream = stream; this.opts = opts; this.state = "inactive";
    this.ondataavailable = null; this.onstop = null;
  }
  static isTypeSupported() { return true; }
  start() { this.state = "recording"; recorded.push("start"); }
  stop() {
    this.state = "inactive";
    recorded.push("stop");
    setTimeout(() => {
      this.ondataavailable({ data: { size: 11 } });
      this.onstop();
    }, 0);
  }
};
setNavigator({ mediaDevices: {
  getUserMedia: async () => ({ getTracks: () => [{ stop: () => {} }] }),
} });
let fetchCalls = 0;
global.fetch = async () => {
  fetchCalls++;
  return { ok: true, status: 200, json: async () => ({ text: "spoken question" }) };
};

await onMicClick();               // first press
assert.deepStrictEqual(recorded, ["start"], "first press starts recording");
assert.strictEqual(voice.state, "listening", "state is listening");
assertIncludes($("voice-notice").textContent, "press again", "tells the user what is next");
assert.strictEqual($("mic").textContent, "■", "button shows stop");

await onMicClick();               // second press
assert.deepStrictEqual(recorded, ["start", "stop"], "second press stops");

// An impatient third press, while transcription is still in flight, must be
// ignored rather than starting an overlapping recording.
await onMicClick();
assert.deepStrictEqual(recorded, ["start", "stop"], "press while transcribing is ignored");
assert.strictEqual($("mic").disabled, true, "mic disabled while transcribing");

await tick(); await tick();       // the browser's asynchronous final events
assert.strictEqual($("input").value, "spoken question",
  "recorded audio survived the stop and reached the provider");
assert.strictEqual(fetchCalls, 1, "transcribed exactly once");

console.log("ui voice assertions passed");
process.exit(0);

})();
"""


def _ui_script():
    html = open(UI, encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline script found in " + UI
    return "\n".join(blocks)


def _run(harness_body, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    path = tmp_path / "voice_harness.js"
    path.write_text(HARNESS + _ui_script() + harness_body, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_ui_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    path = tmp_path / "ui.js"
    path.write_text(_ui_script(), encoding="utf-8")
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_voice_recorder_and_playback_behaviour(tmp_path):
    out = _run(ASSERTIONS, tmp_path)
    assert "ui voice assertions passed" in out


def test_mic_and_speaker_controls_exist():
    """The controls have to be in the markup, not only in the script."""
    html = open(UI, encoding="utf-8").read()
    assert 'id="mic"' in html
    assert 'id="voice-auto"' in html
    assert 'id="voice-notice"' in html
    # The mic belongs in the composer, next to the input, not on the auth screen.
    composer = html.index('id="composer"')
    assert composer < html.index('id="mic"') < html.index('id="send"')
