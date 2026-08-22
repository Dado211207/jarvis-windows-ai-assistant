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
// What this keeps: six numbers of state, each overwritten every block.
// No ring buffer, no history, no samples retained past the call that
// produced them.
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

class ClapProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const given = (options && options.processorOptions) || {};
    for (const key of Object.keys(DEFAULTS)) {
      const value = Number(given[key]);
      this[key] = Number.isFinite(value) && value > 0 ? value : DEFAULTS[key];
    }

    this.noiseFloor = 0.002;
    this.prevRms = 0;
    this.inTransient = false;
    this.transientStart = 0;
    this.lastClapAt = -999;
    this.mutedUntil = -999;
    this.sustainedUntil = -999;
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
      const sharp = this.prevRms < threshold * this.attackFall;
      if (rms >= threshold && peak >= this.absMin && sharp && now >= this.sustainedUntil) {
        this.inTransient = true;
        this.transientStart = now;
      } else if (rms < threshold) {
        // Track the background only while nothing is happening, or a
        // clap would raise the floor it is measured against.
        this.noiseFloor = this.noiseFloor * 0.995 + rms * 0.005;
      }
    }

    this.prevRms = rms;
    return true;
  }

  _clap(at) {
    if (at < this.mutedUntil) return;
    const gap = at - this.lastClapAt;
    if (gap >= this.minGap && gap <= this.maxGap) {
      this.lastClapAt = -999;
      this.mutedUntil = at + this.refractory;
      this.port.postMessage({ type: "clap-pair" });
    } else {
      // A single clap, or a mistimed second one. Remembered as the
      // possible first half of a pair; never reported.
      this.lastClapAt = at;
    }
  }
}

registerProcessor("clap-processor", ClapProcessor);
