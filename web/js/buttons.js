// The hardware buttons of the toolbar. A press is a down and an up, so that
// a held button works: a one-second hold of Power opens the power menu.

/**
 * Buttons carry data-button="HOME" and so on. `send(message)` and
 * `enabled()` are as for the display input.
 */
export function attachButtons(root, { send, enabled, onInput }) {
  const held = new Map();

  const message = (name, action) => ({ type: 'button', name, action });
  const emit = (name, action) => {
    if (enabled() && send(message(name, action))) onInput?.();
  };
  const release = pointerId => {
    const name = held.get(pointerId);
    if (name === undefined) return;
    held.delete(pointerId);
    emit(name, 'up');
  };

  root.addEventListener('pointerdown', e => {
    const button = e.target.closest('[data-button]');
    if (!button || button.disabled || e.button !== 0) return;
    button.setPointerCapture(e.pointerId);
    held.set(e.pointerId, button.dataset.button);
    emit(button.dataset.button, 'down');
  });
  root.addEventListener('pointerup', e => release(e.pointerId));
  root.addEventListener('pointercancel', e => release(e.pointerId));
  // The pointer sent its own down and up. A click without a pointer, from
  // the keyboard, has no detail and is a whole press.
  root.addEventListener('click', e => {
    const button = e.target.closest('[data-button]');
    if (button && e.detail === 0) emit(button.dataset.button, 'press');
  });
  root.addEventListener('contextmenu', e => e.preventDefault());

  return {
    releaseAll() {
      [...held.keys()].forEach(release);
    },
  };
}
