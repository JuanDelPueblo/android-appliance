// Keyboard input: which browser key events reach Android, and how.
//
// Printable ASCII goes as a key press. Named keys (Enter, arrows, modifiers)
// go as down and up, so that a held key works. Text that no key press can
// carry (IME results, non-ASCII characters, pastes) goes as a text message,
// which the daemon pastes through the guest clipboard.

// CapsLock sticks in the guest and inverts the case of every letter. The
// others are steps of a composition: its result arrives as text.
const IGNORED = new Set(['CapsLock', 'Dead', 'Process', 'Unidentified']);

const isAscii = char => char >= ' ' && char <= '~';

/**
 * What to do with a keydown: {kind: 'ignore'} (leave it to the browser),
 * 'paste' (let the browser paste; the paste event delivers the text),
 * 'press' (printable ASCII), 'text' (any other single character) or
 * 'named'.
 */
export function classifyKey(event) {
  const { key } = event;
  // keyCode 229 is how browsers mark a key that an IME consumed.
  if (event.isComposing || event.keyCode === 229 || IGNORED.has(key)) return { kind: 'ignore' };
  if ((event.ctrlKey || event.metaKey) && !event.altKey && key.toLowerCase() === 'v') return { kind: 'paste' };
  const chars = [...key];
  if (chars.length !== 1) return { kind: 'named', key };
  if (isAscii(key)) return { kind: 'press', key };
  // AltGr reports Ctrl+Alt; Ctrl or Meta alone make a shortcut, not text.
  if (event.metaKey || (event.ctrlKey && !event.altKey)) return { kind: 'ignore' };
  return { kind: 'text', text: key };
}

// The daemon takes at most 64 K characters in a text message.
export const MAX_TEXT = 60_000;

/** The text in pieces that fit a message; a pair of surrogates stays whole. */
export function chunkText(text, size = MAX_TEXT) {
  const chunks = [];
  for (let start = 0; start < text.length; ) {
    let end = Math.min(text.length, start + size);
    const last = text.charCodeAt(end - 1);
    if (end < text.length && last >= 0xd800 && last <= 0xdbff) end--;
    chunks.push(text.slice(start, end));
    start = end;
  }
  return chunks;
}

const keyMessage = (key, code, action) => ({ type: 'key', key, code, action });

/**
 * Turns keydown and keyup events into messages and remembers the keys that
 * are down, so that a lost focus can release them.
 */
export class KeyTracker {
  #held = new Map();

  /** Returns {handled, messages}; `handled` means: prevent the default. */
  keydown(event) {
    const result = classifyKey(event);
    const code = event.code || event.key;
    switch (result.kind) {
      case 'press':
        return { handled: true, messages: [keyMessage(result.key, event.code, 'press')] };
      case 'text':
        return { handled: true, messages: [{ type: 'text', text: result.text }] };
      case 'named':
        // Android repeats a held key itself.
        if (event.repeat) return { handled: true, messages: [] };
        this.#held.set(code, { key: result.key, code: event.code });
        return { handled: true, messages: [keyMessage(result.key, event.code, 'down')] };
      default:
        return { handled: false, messages: [] };
    }
  }

  keyup(event) {
    const code = event.code || event.key;
    const held = this.#held.get(code);
    if (held) {
      this.#held.delete(code);
      return { handled: true, messages: [keyMessage(held.key, held.code, 'up')] };
    }
    // The press of a printable key was complete at keydown.
    const { kind } = classifyKey(event);
    return { handled: kind === 'press' || kind === 'text', messages: [] };
  }

  releaseAll() {
    const messages = [...this.#held.values()].map(({ key, code }) => keyMessage(key, code, 'up'));
    this.#held.clear();
    return messages;
  }
}

/**
 * The beforeinput events of a keyboard that sends no usable key events
 * (a phone's soft keyboard). Returns a message or null.
 */
export function inputMessage({ inputType, data, isComposing }) {
  // A composition delivers its result at compositionend.
  if (isComposing) return null;
  switch (inputType) {
    case 'insertText':
    case 'insertReplacementText':
      return data ? { type: 'text', text: data } : null;
    case 'insertLineBreak':
    case 'insertParagraph':
      return keyMessage('Enter', 'Enter', 'press');
    case 'deleteContentBackward':
      return keyMessage('Backspace', 'Backspace', 'press');
    case 'deleteContentForward':
      return keyMessage('Delete', 'Delete', 'press');
    default:
      return null;
  }
}
