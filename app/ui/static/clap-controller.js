// The double-clap listener's state machine — one owner for one microphone.
//
// Read app/voice/clap.py first: it carries the privacy boundary this code
// has to hold up. This file is the browser half, and it exists as a single
// controller rather than a handful of booleans in app.js because the
// booleans were the bug. Privacy mode, push-to-talk, spoken replies,
// calibration, device changes, navigation and quit can each want the
// microphone stopped, and they arrive interleaved. Anything less than one
// explicit state machine produces the failure this pass was opened to fix:
// privacy mode switched on somewhere else, and the microphone kept running.
//
// It is longer than this project's usual ~200-line guidance on purpose.
// Splitting it would reintroduce exactly the scattered, racing flags the
// requirement forbids; the cohesion is the point.
//
// **What it still never does.** It reads amplitude, never content. The
// worklet's normal output is one payload-free message. Calibration adds
// per-onset scalars — a time, a peak level, a gap — and those never leave
// the page: nothing here posts them anywhere, which app.js's own tests
// assert. No recording, no buffer, no upload, no recognition.

const ClapController = (function () {
  "use strict";

  const STATE = {
    DISABLED: "disabled",
    STARTING: "starting",
    LISTENING: "listening",
    SUSPENDED: "suspended",
    CALIBRATING: "calibrating",
    PRIVACY_BLOCKED: "privacy-blocked",
    MIC_UNAVAILABLE: "microphone-unavailable",
    STOPPING: "stopping",
    ERROR: "error",
  };

  // How long to wait after the last suspension reason clears before
  // reopening the microphone. Speakers click and rooms ring; restarting
  // instantly is how JARVIS's own last syllable becomes a clap.
  const RESUME_DELAY_MS = 450;

  // A calibration session may never outlive this, whatever happens.
  const CALIBRATION_MAX_MS = 15000;

  let state = STATE.DISABLED;
  let settings = { enabled: false, privacyBlocked: false, detector: {}, deviceId: "" };

  // The four things that must be released. Held together so teardown
  // cannot forget one.
  let stream = null;
  let context = null;
  let node = null;
  let source = null;

  // Every start() carries the generation it began in. A start that
  // resolves after a teardown belongs to a world that no longer exists
  // and disposes of itself instead of installing a live microphone.
  let generation = 0;

  const reasons = new Set();
  let resumeTimer = null;
  let calibration = null;
  let quitting = false;
  let activeDeviceId = "";
  let usingFallback = false;
  let deviceChangeBound = false;
  const listeners = [];

  // ── Resource teardown ────────────────────────────────────────────────
  //
  // Synchronous and idempotent. Called from privacy, suspension, device
  // change, navigation and quit, so it may never assume which of the four
  // resources exist.

  function teardown() {
    generation += 1;
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = null;
    }
    if (node) {
      try {
        node.port.onmessage = null;
        node.disconnect();
      } catch (e) { /* already gone */ }
      node = null;
    }
    if (source) {
      try { source.disconnect(); } catch (e) { /* already gone */ }
      source = null;
    }
    if (stream) {
      // Stopping every track is what actually turns the Windows
      // microphone-in-use indicator off. Closing the context alone does
      // not.
      stream.getTracks().forEach(function (track) {
        try { track.stop(); } catch (e) { /* already stopped */ }
      });
      stream = null;
    }
    if (context) {
      try { context.close(); } catch (e) { /* already closed */ }
      context = null;
    }
    activeDeviceId = "";
    usingFallback = false;
  }

  // The honest answer to "is the microphone open right now" — an active
  // worklet AND a live audio track. The tray is not allowed to say "On"
  // for anything less.
  function isListening() {
    if (!node || !stream) return false;
    return stream.getAudioTracks().some(function (t) { return t.readyState === "live"; });
  }

  function setState(next) {
    if (state === next) return;
    state = next;
    notify();
  }

  function notify() {
    const snapshot = {
      state: state,
      listening: isListening(),
      deviceId: activeDeviceId,
      usingFallback: usingFallback,
      suspendedBy: Array.from(reasons),
    };
    listeners.forEach(function (fn) {
      try { fn(snapshot); } catch (e) { /* a listener must not break the machine */ }
    });
  }

  // ── Opening the microphone ───────────────────────────────────────────

  function constraintsFor(deviceId) {
    // A clap is a transient. The three processors that exist to make
    // speech intelligible all work against that: gain control chases the
    // peak, noise suppression treats a broadband burst as noise, and echo
    // cancellation is irrelevant to a sound with no far end.
    const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
    if (deviceId) audio.deviceId = { exact: deviceId };
    return { audio: audio };
  }

  async function openStream(deviceId) {
    if (!deviceId) {
      return { stream: await navigator.mediaDevices.getUserMedia(constraintsFor("")), fallback: false };
    }
    try {
      return { stream: await navigator.mediaDevices.getUserMedia(constraintsFor(deviceId)), fallback: false };
    } catch (e) {
      // The chosen microphone is gone, renamed, or in use. Falling back
      // to the default is right; pretending the chosen one is active is
      // not, so the caller is told which happened.
      const missing = e && (e.name === "OverconstrainedError" || e.name === "NotFoundError");
      if (!missing) throw e;
      return { stream: await navigator.mediaDevices.getUserMedia(constraintsFor("")), fallback: true };
    }
  }

  async function start(processorOptions, onMessage) {
    const mine = generation;
    const opened = await openStream(settings.deviceId);
    if (mine !== generation) {           // torn down while we were waiting
      opened.stream.getTracks().forEach(function (t) { t.stop(); });
      return false;
    }
    stream = opened.stream;
    usingFallback = opened.fallback;
    const track = stream.getAudioTracks()[0];
    activeDeviceId = (track && track.getSettings && track.getSettings().deviceId) || "";

    context = new AudioContext();
    await context.audioWorklet.addModule("/ui/static/clap-processor.js");
    if (mine !== generation) {           // torn down during addModule
      teardown();
      return false;
    }
    source = context.createMediaStreamSource(stream);
    node = new AudioWorkletNode(context, "clap-processor", { processorOptions: processorOptions });
    node.port.onmessage = onMessage;
    // Deliberately not connected to the destination: this reads the
    // microphone, it does not play it back.
    source.connect(node);
    return true;
  }

  // ── The single decision point ────────────────────────────────────────
  //
  // Everything that can want the microphone stopped or started ends here.
  // Nothing else may call start() or teardown() directly.

  let reconciling = false;

  async function reconcile() {
    if (reconciling) return;
    reconciling = true;
    try {
      if (quitting) { teardown(); setState(STATE.DISABLED); return; }
      if (calibration) { setState(STATE.CALIBRATING); return; }
      if (!settings.enabled) { teardown(); setState(STATE.DISABLED); return; }
      if (settings.privacyBlocked) { teardown(); setState(STATE.PRIVACY_BLOCKED); return; }
      if (reasons.size > 0) { teardown(); setState(STATE.SUSPENDED); return; }
      if (isListening()) { setState(STATE.LISTENING); return; }

      // Bound here rather than after a successful start, so a machine
      // with no microphone at all still hears about one being plugged in.
      // Binding it inside start() meant "microphone unavailable" was
      // permanent until the page was reloaded.
      bindDeviceChange();

      teardown();                        // never stack two streams
      if (!supported()) { setState(STATE.MIC_UNAVAILABLE); return; }
      setState(STATE.STARTING);
      try {
        const ok = await start(settings.detector || {}, onWorkletMessage);
        if (!ok) return;                 // superseded; whoever superseded us sets the state
        setState(STATE.LISTENING);
      } catch (e) {
        teardown();
        setState(STATE.MIC_UNAVAILABLE);
      }
    } finally {
      reconciling = false;
      notify();
    }
  }

  function supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.AudioContext);
  }

  // ── Messages from the audio thread ───────────────────────────────────

  let onPair = function () {};

  function onWorkletMessage(event) {
    const data = event && event.data;
    if (!data) return;
    if (data.type === "clap-pair") {
      // A pair that arrives while anything wants the microphone quiet is
      // discarded rather than acted on — the block below is what makes
      // "a clap during privacy mode does nothing" true even in the
      // moment between the setting changing and teardown finishing.
      if (quitting || !settings.enabled || settings.privacyBlocked || reasons.size > 0) return;
      onPair();
    }
  }

  // ── Device changes ───────────────────────────────────────────────────

  function bindDeviceChange() {
    if (deviceChangeBound || !navigator.mediaDevices || !navigator.mediaDevices.addEventListener) return;
    navigator.mediaDevices.addEventListener("devicechange", onDeviceChange);
    deviceChangeBound = true;
  }

  async function onDeviceChange() {
    // Restart only when the microphone actually in use has gone away.
    // Plugging in a webcam is not a reason to reopen an audio stream.
    if (!isListening()) { reconcile(); return; }
    let devices = [];
    try {
      devices = await navigator.mediaDevices.enumerateDevices();
    } catch (e) {
      return;
    }
    const stillThere = devices.some(function (d) {
      return d.kind === "audioinput" && d.deviceId === activeDeviceId;
    });
    if (!stillThere) {
      teardown();
      reconcile();
    }
  }

  // ── Calibration ──────────────────────────────────────────────────────
  //
  // Bounded, explicitly started, and it owns the microphone for its
  // duration so there is never a second consumer. Onset scalars are
  // handed to the caller and go nowhere else.

  async function startCalibration(handlers) {
    stopCalibration();
    teardown();
    if (quitting || settings.privacyBlocked || !supported()) {
      return { ok: false, reason: settings.privacyBlocked ? "privacy" : "unsupported" };
    }
    calibration = {
      onsets: [],
      done: false,
      handlers: handlers || {},
      timer: null,
    };
    setState(STATE.CALIBRATING);
    try {
      const opts = Object.assign({}, settings.detector || {}, { calibrate: true });
      const ok = await start(opts, onCalibrationMessage);
      if (!ok) { calibration = null; return { ok: false, reason: "superseded" }; }
    } catch (e) {
      calibration = null;
      teardown();
      setState(STATE.MIC_UNAVAILABLE);
      return { ok: false, reason: "microphone" };
    }
    calibration.timer = setTimeout(function () { finishCalibration("timeout"); }, CALIBRATION_MAX_MS);
    notify();
    return { ok: true, usingFallback: usingFallback, deviceId: activeDeviceId };
  }

  function onCalibrationMessage(event) {
    const data = event && event.data;
    if (!calibration || !data) return;
    if (data.type === "clap-onset") {
      calibration.onsets.push({ at: data.at, peak: data.peak, gap: data.gap });
      if (calibration.handlers.onOnset) {
        try { calibration.handlers.onOnset(calibration.onsets.slice()); } catch (e) { /* ignore */ }
      }
      if (calibration.onsets.length >= 2) finishCalibration("done");
    }
  }

  function finishCalibration(outcome) {
    if (!calibration || calibration.done) return;
    calibration.done = true;
    if (calibration.timer) clearTimeout(calibration.timer);
    const onsets = calibration.onsets.slice();
    const handlers = calibration.handlers;
    calibration = null;
    teardown();
    if (handlers.onFinish) {
      try { handlers.onFinish(outcome, onsets); } catch (e) { /* ignore */ }
    }
    reconcile();
  }

  function stopCalibration() {
    if (calibration) finishCalibration("cancelled");
  }

  // ── Public surface ───────────────────────────────────────────────────

  return {
    STATE: STATE,
    RESUME_DELAY_MS: RESUME_DELAY_MS,
    CALIBRATION_MAX_MS: CALIBRATION_MAX_MS,

    state: function () { return state; },
    isListening: isListening,
    activeDeviceId: function () { return activeDeviceId; },
    usingFallback: function () { return usingFallback; },
    suspendedBy: function () { return Array.from(reasons); },
    onChange: function (fn) { listeners.push(fn); },
    onClapPair: function (fn) { onPair = fn; },

    // Apply what the server says. `deviceId` is the shared microphone
    // choice; changing it restarts onto the new device, stopping the old
    // stream first.
    configure: async function (next) {
      const before = settings;
      settings = {
        enabled: !!next.enabled,
        privacyBlocked: !!next.privacyBlocked,
        detector: next.detector || {},
        deviceId: next.deviceId || "",
      };
      const changed =
        before.deviceId !== settings.deviceId ||
        JSON.stringify(before.detector) !== JSON.stringify(settings.detector);
      if (changed && isListening()) teardown();
      if (next.forceRestart && isListening()) teardown();
      await reconcile();
    },

    // Privacy is not a request — it is immediate. Tear the microphone
    // down synchronously, then let reconcile() settle the state, so there
    // is no window in which the indicator is still lit.
    setPrivacyBlocked: async function (blocked) {
      settings.privacyBlocked = !!blocked;
      if (settings.privacyBlocked) {
        teardown();
        stopCalibration();
        setState(STATE.PRIVACY_BLOCKED);
      }
      await reconcile();
    },

    // Reference-counted. Two reasons in, one reason out, still suspended.
    suspend: function (reason) {
      if (!reason) return;
      const first = reasons.size === 0;
      reasons.add(reason);
      if (first) {
        teardown();
        if (settings.enabled && !settings.privacyBlocked) setState(STATE.SUSPENDED);
        notify();
      }
    },

    resume: function (reason) {
      if (!reason || !reasons.has(reason)) return;
      reasons.delete(reason);
      if (reasons.size > 0) return;      // another reason still holds it
      if (resumeTimer) clearTimeout(resumeTimer);
      resumeTimer = setTimeout(function () {
        resumeTimer = null;
        reconcile();
      }, RESUME_DELAY_MS);
    },

    startCalibration: startCalibration,
    stopCalibration: stopCalibration,

    stop: function () { stopCalibration(); teardown(); setState(STATE.DISABLED); notify(); },

    setQuitting: function () { quitting = true; stopCalibration(); teardown(); setState(STATE.DISABLED); notify(); },

    // A page restored from the back/forward cache is alive again, and its
    // own `pagehide` already ran. Without this, Back into the Voice page
    // would find `quitting` still true from that pagehide and never
    // listen again. Whether a page holding a microphone is ever cached is
    // a browser decision that changes between versions; this costs
    // nothing when it is not.
    setRestored: function () { quitting = false; },

    // Tests only: forget everything so one case cannot colour the next.
    _resetForTests: function () {
      quitting = false;
      reasons.clear();
      stopCalibration();
      teardown();
      settings = { enabled: false, privacyBlocked: false, detector: {}, deviceId: "" };
      state = STATE.DISABLED;
    },
  };
})();

if (typeof window !== "undefined") window.ClapController = ClapController;
