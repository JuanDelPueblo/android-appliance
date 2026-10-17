// What the connection panel shows, derived from RTCPeerConnection.getStats().

/** The reports of the video stream, the codec and the selected pair. */
export function pickStats(reports) {
  const byId = new Map(reports.map(r => [r.id, r]));
  const video = reports.find(r => r.type === 'inbound-rtp' && (r.kind ?? r.mediaType) === 'video');
  const transport = reports.find(r => r.type === 'transport');
  const pair =
    byId.get(transport?.selectedCandidatePairId) ??
    reports.find(r => r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded');
  return { video, pair, codec: video && byId.get(video.codecId) };
}

const perSecond = (cur, prev, field, seconds) => (cur[field] - prev[field]) / seconds;

/**
 * The numbers of the panel. `prev` is the sample before `cur` (or null);
 * a sample is {time (ms), ...pickStats()}.
 */
export function summarize(prev, cur) {
  const { video, pair, codec } = cur;
  if (!video) return { codec: null, width: null, height: null, fps: null, kbps: null, lost: null, decoded: null, dropped: null, rtt: null };
  const before = prev?.video;
  const seconds = before ? (cur.time - prev.time) / 1000 : 0;
  const rate = field => (seconds > 0 && before?.[field] !== undefined ? perSecond(video, before, field, seconds) : null);
  const kbps = rate('bytesReceived');
  return {
    codec: codec?.mimeType?.replace(/^video\//, '') ?? null,
    width: video.frameWidth ?? null,
    height: video.frameHeight ?? null,
    fps: rate('framesDecoded') ?? video.framesPerSecond ?? null,
    kbps: kbps === null ? null : (kbps * 8) / 1000,
    lost: video.packetsLost ?? null,
    decoded: video.framesDecoded ?? null,
    dropped: video.framesDropped ?? null,
    rtt: pair?.currentRoundTripTime === undefined ? null : pair.currentRoundTripTime * 1000,
  };
}

/**
 * Estimates the time from an input to the frame that shows it: the time to
 * the first frame after an input. A frame that comes unrelated to the input
 * makes the estimate low, so it is a guide and not a measurement.
 */
export class LatencyMeter {
  #pending = null;
  #samples = [];
  #size;
  #window;

  constructor(size = 15, window = 1000) {
    this.#size = size;
    this.#window = window;
  }

  input(now) {
    // An input that changed nothing never gets a frame; do not wait for it.
    if (this.#pending === null || now - this.#pending > this.#window) this.#pending = now;
  }

  frame(now) {
    if (this.#pending === null) return;
    if (now - this.#pending <= this.#window) {
      this.#samples.push(now - this.#pending);
      if (this.#samples.length > this.#size) this.#samples.shift();
    }
    this.#pending = null;
  }

  /** The median of the last samples, or null. */
  get median() {
    if (!this.#samples.length) return null;
    const sorted = [...this.#samples].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  }

  reset() {
    this.#pending = null;
    this.#samples = [];
  }
}
