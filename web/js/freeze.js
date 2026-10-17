// Detects a video that stopped while the screen kept changing.
//
// The daemon sends the hash of a small screenshot once per second. The
// emulator sends a frame whenever the display changes, so a hash that
// changed since the previous event must come with a decoded frame. If no
// frame was decoded in that interval and none follows within `timeout`, the
// video is frozen. A screen that does not change sends no frames and no
// hash change, and so is not a freeze.

export const FREEZE_TIMEOUT_MS = 3000;

export class FreezeDetector {
  #timeout;
  #hash = null;
  #frames = 0;
  #framesAtDisplay = 0;
  #pendingSince = null;

  constructor(timeout = FREEZE_TIMEOUT_MS) {
    this.#timeout = timeout;
  }

  /** A display event arrived. */
  display(hash, now) {
    const changed = this.#hash !== null && hash !== this.#hash;
    const framed = this.#frames !== this.#framesAtDisplay;
    this.#hash = hash;
    this.#framesAtDisplay = this.#frames;
    if (changed && !framed && this.#pendingSince === null) this.#pendingSince = now;
  }

  /** The video decoded a frame. */
  frame() {
    this.#frames++;
    this.#pendingSince = null;
  }

  /** True when the video has been frozen for the timeout. */
  frozen(now) {
    return this.#pendingSince !== null && now - this.#pendingSince >= this.#timeout;
  }

  /** Forget everything: a new stream, or a tab that was hidden. */
  reset() {
    this.#hash = null;
    this.#framesAtDisplay = this.#frames;
    this.#pendingSince = null;
  }
}
