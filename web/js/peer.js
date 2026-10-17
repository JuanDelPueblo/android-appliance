// One RTCPeerConnection for one stream. The emulator offers; the dashboard
// only answers. The ICE servers and the transport policy are the ones the
// server sent: media goes through the TURN relay of the appliance, so it
// needs one known port whatever the network.

/**
 * `onSignal(data)` gets what to send to the server (the answer, candidates)
 * and `onState({ice, connection})` the changes of the connection.
 */
export class Peer {
  #pc;
  #queue = Promise.resolve();
  #candidates = [];
  #closed = false;
  #onSignal;

  constructor(config, video, { onSignal, onState }) {
    this.video = video;
    this.#onSignal = onSignal;
    const pc = (this.#pc = new RTCPeerConnection({
      iceServers: config.iceServers,
      iceTransportPolicy: config.iceTransportPolicy,
    }));
    pc.addEventListener('track', e => this.#track(e));
    pc.addEventListener('icecandidate', ({ candidate }) => {
      // The end-of-candidates marker is empty. The emulator cannot parse it.
      if (candidate?.candidate) onSignal({ candidate: candidate.candidate, sdpMid: candidate.sdpMid, sdpMLineIndex: candidate.sdpMLineIndex });
    });
    const report = () => onState({ ice: pc.iceConnectionState, connection: pc.connectionState });
    pc.addEventListener('connectionstatechange', report);
    pc.addEventListener('iceconnectionstatechange', report);
  }

  #track({ track, receiver }) {
    // The audio track (the emulator offers one even with -no-audio) is not played.
    if (track.kind !== 'video') return;
    // Show frames as they arrive: no jitter buffer beyond the minimum.
    if ('jitterBufferTarget' in receiver) receiver.jitterBufferTarget = 0;
    else if ('playoutDelayHint' in receiver) receiver.playoutDelayHint = 0;
    this.video.srcObject = new MediaStream([track]);
    this.video.play().catch(() => {});
  }

  /** The offer, or a candidate of the emulator. Messages are handled in order. */
  signal(data) {
    this.#queue = this.#queue.then(() => this.#apply(data)).catch(() => {});
  }

  async #apply(data) {
    if (this.#closed) return;
    const pc = this.#pc;
    if (data.type === 'offer') {
      await pc.setRemoteDescription({ type: 'offer', sdp: data.sdp });
      for (const candidate of this.#candidates.splice(0)) await pc.addIceCandidate(candidate).catch(() => {});
      await pc.setLocalDescription(await pc.createAnswer());
      this.#onSignal({ type: 'answer', sdp: pc.localDescription.sdp });
    } else if (data.candidate) {
      const candidate = { candidate: data.candidate, sdpMid: data.sdpMid, sdpMLineIndex: data.sdpMLineIndex };
      // Candidates may come before the offer is applied.
      if (pc.remoteDescription) await pc.addIceCandidate(candidate).catch(() => {});
      else this.#candidates.push(candidate);
    }
  }

  /** The raw reports, as plain objects. */
  async stats() {
    return this.#closed ? [] : [...(await this.#pc.getStats()).values()];
  }

  close() {
    if (this.#closed) return;
    this.#closed = true;
    this.#pc.close();
    if (this.video.srcObject) this.video.srcObject = null;
  }
}
