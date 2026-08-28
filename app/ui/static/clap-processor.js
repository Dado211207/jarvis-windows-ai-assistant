// Double-clap detection. Runs on the audio rendering thread, in an
// AudioWorklet, over the same microphone stream the level meter uses.
//
// Read app/voice/clap.py before changing anything here — the boundary
// this feature lives inside is written down there, and it is narrow on
// purpose.
//
// What this computes, per 128-sample block: root-mean-square and peak
// amplitude. Two floats. That is the entire feature set, and it is why
// this is a transient counter and not a listener: there is nothing in
// two amplitude numbers that could distinguish a word from a door.
//
// What this keeps: a handful of numbers of state, each overwritten every
// block — a level, a couple of flags, three timestamps, and one count of
// consecutive above-gate blocks. No ring buffer, no array, no samples
// retained past the call that produced them. The count was added because
// a clap whose attack straddles a block boundary was being missed; it is
// a number of blocks, not audio, and nothing can be reconstructed from it.
//
// What this sends: `{type: "clap-pair"}`, with nothing in it. Not a
// level, not a timestamp, not a sample. The message has no payload
// because there is nothing that would be safe to put in one.
//
// Why an AudioWorklet rather than requestAnimationFrame over an
// AnalyserNode (which is what the diagnostics level meter does): rAF is
// throttled hard in a hidden or backgrounded document, and this has to
// keep working when the JARVIS window is minimised to the tray. The
// audio rendering thread is not throttled.

const DEFAULTS = {
  // How far above the measured background a block must rise.
  floorRatio: 6.0,
  // An absolute floor, so a silent room cannot make this infinitely
  // sensitive as the measured background approaches zero.
  absMin: 0.035,
  // How far *below* threshold the preceding block must have been. This
  // is the discriminator that matters: a clap goes from background to
  // peak inside one 2.7 ms block, and speech ramps over tens of
  // milliseconds, so at the moment speech crosses the threshold its
  // previous block is just under it, not far under it.
  attackFall: 0.35,
  // Longer than this and it is a sustained sound, not a clap.
  maxTransient: 0.16,
  // The window the second clap has to land in.
  minGap: 0.12,
  maxGap: 0.7,
  // Quiet period after firing, so one pair is one activation.
  refractory: 1.5,
};

// How many consecutive above-gate blocks still count as "the signal
// only just rose". Two blocks is 5.3 ms at 48 kHz: long enough to
// cover an attack that straddles a 128-sample boundary plus one
// block of settling, and far shorter than any speech ramp. It is
// deliberately not calibratable — SAFE_BOUNDS covers absMin, minGap
// and maxGap only, and the attack test is not a user's to loosen.
const ATTACK_STRADDLE_BLOCKS = 2;

class ClapProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const given = (options && options.processorOptions) || {};
    for (const key of Object.keys(DEFAULTS)) {
      const value = Number(given[key]);
      this[key] = Number.isFinite(value) && value > 0 ? value : DEFAULTS[key];
    }

    // Calibration mode, and only calibration mode, additionally reports
    // one scalar triple per onset so the Voice page can say "that was
    // too quiet" or "the second clap was too slow" instead of just
    // failing silently. It is off unless a person pressed Calibrate.
    //
    // Three numbers — a time, a peak level, a gap — are strictly less
    // than the amplitude this processor already computes, they are the
    // minimum needed to give useful advice, and app/ui/static/app.js
    // never sends them anywhere. In ordinary listening the only thing
    // that ever leaves this thread is still the payload-free pair below.
    this.calibrate = !!(given && given.calibrate);

    this.noiseFloor = 0.002;
    this.prevRms = 0;
    this.inTransient = false;
    this.transientStart = 0;
    this.transientPeak = 0;
    this.lastClapAt = -999;
    this.mutedUntil = -999;
    this.sustainedUntil = -999;

    // How many consecutive blocks have been above the quiet gate. A
    // count, not a history: one number, overwritten every block, from
    // which no audio can be reconstructed. See the attack test below for
    // why it is needed.
    this.loudBlocks = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;

    let sum = 0;
    let peak = 0;
    for (let i = 0; i < channel.length; i++) {
      const v = channel[i];
      sum += v * v;
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
    }
    const rms = Math.sqrt(sum / channel.length);
    const now = currentTime;
    const threshold = Math.max(this.noiseFloor * this.floorRatio, this.absMin);

    if (this.inTransient) {
      const duration = now - this.transientStart;
      if (peak > this.transientPeak) this.transientPeak = peak;
      if (rms < threshold * 0.5) {
        this.inTransient = false;
        if (duration <= this.maxTransient) this._clap(now - duration);
      } else if (duration > this.maxTransient) {
        // Sustained: speech, music, a running tap. Not a clap — and it
        // must not produce a fresh onset the instant it dips, so the
        // pairing state is cleared and onsets are suppressed briefly.
        this.inTransient = false;
        this.sustainedUntil = now + 0.25;
        this.lastClapAt = -999;
      }
    } else {
      // Was it quiet just before this? Two ways of asking, because one
      // was not enough.
      //
      // The first — "the previous block was well below threshold" — is
      // the discriminator that rejects speech, and it must stay. But a
      // clap's attack does not respect 128-sample boundaries. When it
      // lands near the end of a block, that block carries a sliver of
      // the attack and its RMS rises just above the gate: measured at
      // 0.01268 against a gate of 0.01225, over by 0.00043. `sharp` is
      // then false for the loud block that follows, every later block
      // has an even higher `prevRms`, and the entire clap is swallowed.
      // A first clap lost that way is a double clap that never happens.
      //
      // So a block is also treated as an attack when the signal has only
      // just risen — `loudBlocks` counts consecutive above-gate blocks,
      // and a straddled attack shows up as one or two of them. Speech
      // ramps over tens of milliseconds, so by the time it crosses the
      // threshold it has been above the gate for far longer than this
      // and is still rejected; a swept simulation of every block phase
      // over the suite's own speech, hum, silence, single-clap and
      // mistimed-pair fixtures produces no onset at all.
      const wasQuiet = this.prevRms < threshold * this.attackFall;
      const justRose = this.loudBlocks <= ATTACK_STRADDLE_BLOCKS;
      const sharp = wasQuiet || justRose;
      if (rms >= threshold && peak >= this.absMin && sharp && now >= this.sustainedUntil) {
        this.inTransient = true;
        this.transientStart = now;
        this.transientPeak = peak;
      } else if (rms < threshold) {
        // Track the background only while nothing is happening, or a
        // clap would raise the floor it is measured against.
        this.noiseFloor = this.noiseFloor * 0.995 + rms * 0.005;
      }
    }

    // Maintained here, after the decision, so `loudBlocks` describes the
    // blocks *before* this one — exactly like `prevRms`.
    this.loudBlocks = (rms < threshold * this.attackFall) ? 0 : this.loudBlocks + 1;
    this.prevRms = rms;
    return true;
  }

  _clap(at) {
    if (at < this.mutedUntil) return;
    const gap = at - this.lastClapAt;
    const peak = this.transientPeak;
    if (this.calibrate) {
      // Guarded, and only ever here: three scalars, for a person who
      // pressed Calibrate and is looking at the result.
      this.port.postMessage({ type: "clap-onset", at: at, peak: peak, gap: gap });
    }
    if (gap >= this.minGap && gap <= this.maxGap) {
      this.lastClapAt = -999;
      this.mutedUntil = at + this.refractory;
      this.port.postMessage({ type: "clap-pair" });
    } else {
      // A single clap, or a mistimed second one. Remembered as the
      // possible first half of a pair; never reported outside
      // calibration.
      this.lastClapAt = at;
    }
  }
}

registerProcessor("clap-processor", ClapProcessor);
