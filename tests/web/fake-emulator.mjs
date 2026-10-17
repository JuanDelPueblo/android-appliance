// A stand-in for the emulator's WebRTC side, to test the dashboard's real
// RTCPeerConnection without an emulator. It runs in a page of the same
// browser: it offers a video track that it draws on a canvas, like the
// emulator, only when the picture changes, so a still screen sends no frames.
//
//   const emulator = await startFakeEmulator(context, { width: 1080, height: 1920 });
//   server.onStreamStart = async session => { ... emulator.offer() ... };

export async function startFakeEmulator(context, { width = 1080, height = 1920 } = {}) {
  const page = await context.newPage();
  // Any page of the server will do; the fixture needs only a secure origin.
  await page.goto('about:blank');
  const candidates = [];
  let listener = null;
  await page.exposeFunction('emulatorCandidate', c => {
    if (listener) listener(c);
    else candidates.push(c);
  });
  await page.evaluate(
    ([w, h]) => {
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const g = canvas.getContext('2d');
      const emu = (window.emu = { canvas, g, pc: null, tick: 0, w, h });
      // One picture; each call to paint() changes it.
      emu.paint = () => {
        emu.tick++;
        g.fillStyle = `hsl(${(emu.tick * 37) % 360} 60% 40%)`;
        g.fillRect(0, 0, w, h);
        g.fillStyle = '#fff';
        g.fillRect(w * 0.1, h * 0.1 + (emu.tick % 10) * 20, w * 0.3, h * 0.05);
        // The top left corner is red: a rotated frame is easy to tell.
        g.fillStyle = '#f00';
        g.fillRect(0, 0, w * 0.1, w * 0.1);
      };
      emu.paint();
      emu.stream = canvas.captureStream();
      emu.start = async () => {
        emu.pc?.close();
        const pc = (emu.pc = new RTCPeerConnection({ iceServers: [] }));
        pc.addTrack(emu.stream.getVideoTracks()[0], emu.stream);
        pc.onicecandidate = e => e.candidate?.candidate && window.emulatorCandidate({ candidate: e.candidate.candidate, sdpMid: e.candidate.sdpMid, sdpMLineIndex: e.candidate.sdpMLineIndex });
        await pc.setLocalDescription(await pc.createOffer());
        return pc.localDescription.sdp;
      };
    },
    [width, height],
  );

  return {
    page,
    /** A new peer connection, and its offer. */
    offer: () => page.evaluate(() => window.emu.start()),
    answer: sdp => page.evaluate(sdp => window.emu.pc.setRemoteDescription({ type: 'answer', sdp }), sdp),
    addCandidate: c => page.evaluate(c => window.emu.pc.addIceCandidate(c), c),
    /** Called with each candidate of the emulator. */
    onCandidate(callback) {
      listener = callback;
      candidates.splice(0).forEach(callback);
    },
    /** Change the picture once: the emulator sends one frame. */
    paint: () => page.evaluate(() => window.emu.paint()),
    /** Change the picture 30 times a second until stop(). */
    animate: () => page.evaluate(() => (window.emu.timer ??= setInterval(() => window.emu.paint(), 33)) && true),
    stop: () => page.evaluate(() => { clearInterval(window.emu.timer); window.emu.timer = null; }),
    connectionState: () => page.evaluate(() => window.emu.pc?.connectionState ?? 'none'),
    close: () => page.close(),
  };
}
