// Where a pointer lands on the device.
//
// The video element letterboxes the decoded frame (object-fit: contain), so
// the frame does not always fill the element. Android takes touch in the
// natural frame of the panel (1080x1920), whatever the guest rotation:
// screenshots stay in that frame with the content drawn sideways. A decoded
// frame in the other orientation is therefore the natural frame turned, and
// the point has to be turned back.

/**
 * The area of the element that the frame covers.
 * `rect` is {left, top, width, height}; `frame` is {width, height}.
 */
export function contentRect(rect, frame) {
  const scale = Math.min(rect.width / frame.width, rect.height / frame.height);
  const width = frame.width * scale;
  const height = frame.height * scale;
  return {
    left: rect.left + (rect.width - width) / 2,
    top: rect.top + (rect.height - height) / 2,
    width,
    height,
  };
}

/**
 * Quarter turns, clockwise, from the natural frame to the decoded frame.
 * Same orientation: 0. The other orientation: 3, the guest's landscape
 * (user_rotation=1), where natural = (width - 1 - ly, lx) was measured on
 * Android for a point (lx, ly) of the landscape frame.
 */
export function frameTurns(frame, device) {
  return frame.width >= frame.height === device.width >= device.height ? 0 : 3;
}

// Fractions of the natural frame for fractions (u, v) of the decoded frame.
const UNTURN = [
  (u, v) => [u, v],
  (u, v) => [v, 1 - u],
  (u, v) => [1 - u, 1 - v],
  (u, v) => [1 - v, u],
];

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

/**
 * Map a client position to device pixels.
 *
 * `rect` is the element's bounding rect, `frame` the decoded frame (the
 * device frame while nothing is decoded) and `device` the natural frame.
 * Returns the clamped integer position, and whether the pointer was on the
 * frame rather than on a letterbox bar.
 */
export function mapPoint({ clientX, clientY, rect, frame, device, turns = frameTurns(frame, device) }) {
  const area = contentRect(rect, frame);
  const u = (clientX - area.left) / area.width;
  const v = (clientY - area.top) / area.height;
  const inside = u >= 0 && u <= 1 && v >= 0 && v <= 1;
  const [fx, fy] = UNTURN[turns](clamp(u, 0, 1), clamp(v, 0, 1));
  return {
    x: clamp(Math.floor(fx * device.width), 0, device.width - 1),
    y: clamp(Math.floor(fy * device.height), 0, device.height - 1),
    inside,
  };
}
