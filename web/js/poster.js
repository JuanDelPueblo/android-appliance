// The poster: a screenshot of the display, shown until the video has a frame.
//
// It must never start Android (the daemon boots a stopped Android for a
// screenshot), so it is fetched only while Android runs. It must not keep a
// running Android awake either: a screenshot counts as use. So it is
// fetched for a short while after a stream starts, and again when the
// screen changes without a frame, and not on and on.

import { screenshot } from './api.js';

const REFRESH_MS = 4000;
// How many refreshes follow the first one before the poster rests.
const REFRESHES = 8;

export class Poster {
  #img;
  #onChange;
  #wanted = false;
  #timer = 0;
  #abort = null;
  #left = 0;
  #hash = null;
  /** Whether a screenshot is on the poster. */
  loaded = false;

  constructor(img, onChange) {
    this.#img = img;
    this.#onChange = onChange;
  }

  /** The poster is wanted while Android runs and the video has no frame. */
  want(wanted) {
    if (wanted === this.#wanted) return;
    this.#wanted = wanted;
    if (wanted) {
      this.#left = REFRESHES;
      this.#hash = null;
      this.refresh();
      this.#timer = setInterval(() => this.#left-- > 0 && this.refresh(), REFRESH_MS);
    } else {
      clearInterval(this.#timer);
      this.#abort?.abort();
    }
  }

  /** Somebody is looking again (a reconnect, a tab brought back): refresh for a while. */
  rearm() {
    this.#left = REFRESHES;
  }

  /** The hash of the display: a screen that changed has a new poster. */
  display(hash) {
    if (this.#wanted && this.#hash !== null && hash !== this.#hash) this.refresh();
    this.#hash = hash;
  }

  async refresh() {
    this.#abort?.abort();
    const own = (this.#abort = new AbortController());
    try {
      const url = URL.createObjectURL(await screenshot(own.signal));
      if (own.signal.aborted) {
        URL.revokeObjectURL(url);
        return;
      }
      this.#replace(url);
    } catch {
      // The next refresh tries again.
    }
  }

  #replace(url) {
    const old = this.#img.src;
    this.#img.src = url;
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
    this.loaded = true;
    this.#onChange();
  }

  /** Forget the picture: a stopped Android shows nothing of the last one. */
  clear() {
    if (!this.loaded) return;
    this.loaded = false;
    URL.revokeObjectURL(this.#img.src);
    this.#img.removeAttribute('src');
  }
}
