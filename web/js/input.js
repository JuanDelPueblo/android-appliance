// Pointer, wheel and keyboard input on the display.
//
// Positions go to the daemon in device pixels. Moves and wheel turns are
// sent once per animation frame; presses and releases go at once, after the
// moves that came before them, so that a gesture keeps its order.

import { mapPoint } from './geometry.js';
import { chunkText, inputMessage, KeyTracker } from './keys.js';
import { TouchTable, touchPressure } from './touches.js';

// WheelEvent deltaMode 1 counts lines and 2 pages; the daemon takes pixels.
const LINE_PX = 40;

const round1 = value => Math.round(value * 10) / 10;

export function wheelPixels(event, pageHeight) {
  return event.deltaMode === 1 ? LINE_PX : event.deltaMode === 2 ? pageHeight : 1;
}

export class DisplayInput {
  #touches = new TouchTable();
  #keys = new KeyTracker();
  #mouse = null;
  #wheel = null;
  #touchesMoved = false;
  #frame = 0;
  #o;
  capture;

  /**
   * screen:  the element that gets pointer events
   * sink:    a textarea that holds the keyboard focus and gets text input
   * geometry(): {rect, frame, device} of the video element right now
   * send(message): false when it could not be sent
   * enabled(): whether Android can take input
   * onInput(): called for each message that went out
   * capture: whether the keyboard goes to Android from the start
   * onCapture(on): the keyboard capture changed
   */
  constructor(options) {
    this.#o = options;
    this.capture = options.capture;
    const { screen, sink } = options;
    screen.addEventListener('pointerdown', e => this.#down(e));
    screen.addEventListener('pointermove', e => this.#move(e));
    screen.addEventListener('pointerup', e => this.#up(e, false));
    screen.addEventListener('pointercancel', e => this.#up(e, true));
    screen.addEventListener('lostpointercapture', e => this.#up(e, true));
    screen.addEventListener('wheel', e => this.#onWheel(e), { passive: false });
    screen.addEventListener('contextmenu', e => e.preventDefault());
    screen.addEventListener('dragstart', e => e.preventDefault());
    sink.addEventListener('keydown', e => this.#keydown(e));
    sink.addEventListener('keyup', e => this.#keyup(e));
    sink.addEventListener('beforeinput', e => this.#beforeInput(e));
    sink.addEventListener('compositionend', e => this.#text(e.data));
    sink.addEventListener('paste', e => this.#paste(e));
    // Whatever is left in the sink was sent, or is the middle of a composition.
    sink.addEventListener('input', e => {
      if (!e.isComposing) sink.value = '';
    });
    sink.addEventListener('blur', () => this.#releaseKeys());
  }

  #send(message) {
    if (message && this.#o.enabled() && this.#o.send(message)) this.#o.onInput?.();
  }

  #map(event) {
    return mapPoint({ clientX: event.clientX, clientY: event.clientY, ...this.#o.geometry() });
  }

  // Pointers

  #down(e) {
    if (!this.#o.enabled()) return;
    const point = this.#map(e);
    if (!point.inside) return;
    e.preventDefault();
    this.focus();
    if (e.pointerType === 'mouse') {
      if (this.#mouse) return;
      this.#o.screen.setPointerCapture(e.pointerId);
      this.#mouse = { pointerId: e.pointerId, x: point.x, y: point.y, buttons: e.buttons, dirty: false };
      this.#send({ type: 'mouse', x: point.x, y: point.y, buttons: e.buttons });
    } else {
      this.#flush();
      const message = this.#touches.down(e.pointerId, point.x, point.y, touchPressure(e.pressure));
      if (!message) return;
      this.#o.screen.setPointerCapture(e.pointerId);
      this.#send(message);
    }
  }

  #move(e) {
    const mouse = this.#mouse;
    if (mouse?.pointerId === e.pointerId) {
      const { x, y } = this.#map(e);
      Object.assign(mouse, { x, y });
      if (e.buttons !== mouse.buttons) {
        // Another button went down or up during the drag.
        this.#flush();
        mouse.buttons = e.buttons;
        this.#send({ type: 'mouse', x, y, buttons: e.buttons });
        if (!e.buttons) this.#mouse = null;
      } else {
        mouse.dirty = true;
        this.#schedule();
      }
    } else if (this.#touches.has(e.pointerId)) {
      const { x, y } = this.#map(e);
      if (this.#touches.move(e.pointerId, x, y, touchPressure(e.pressure))) {
        this.#touchesMoved = true;
        this.#schedule();
      }
    }
  }

  #up(e, cancelled) {
    const mouse = this.#mouse;
    if (mouse?.pointerId === e.pointerId) {
      this.#flush();
      const { x, y } = cancelled ? mouse : this.#map(e);
      const buttons = cancelled ? 0 : e.buttons;
      this.#send({ type: 'mouse', x, y, buttons });
      if (!buttons) this.#mouse = null;
      else Object.assign(mouse, { x, y, buttons });
    } else if (this.#touches.has(e.pointerId)) {
      this.#flush();
      const { x, y } = cancelled ? {} : this.#map(e);
      this.#send(this.#touches.up(e.pointerId, x, y));
    }
  }

  #onWheel(e) {
    // Ctrl+wheel is the browser's own zoom (and a trackpad pinch).
    if (e.ctrlKey || !this.#o.enabled()) return;
    e.preventDefault();
    const { x, y } = this.#map(e);
    const pixels = wheelPixels(e, this.#o.screen.clientHeight);
    const wheel = (this.#wheel ??= { dx: 0, dy: 0 });
    Object.assign(wheel, { x, y });
    wheel.dx += e.deltaX * pixels;
    wheel.dy += e.deltaY * pixels;
    this.#schedule();
  }

  #schedule() {
    this.#frame ||= requestAnimationFrame(() => {
      this.#frame = 0;
      this.#flush();
    });
  }

  /** Send what the animation frame has not sent yet. */
  #flush() {
    if (this.#frame) {
      cancelAnimationFrame(this.#frame);
      this.#frame = 0;
    }
    if (this.#touchesMoved) {
      this.#touchesMoved = false;
      this.#send(this.#touches.snapshot());
    }
    const mouse = this.#mouse;
    if (mouse?.dirty) {
      mouse.dirty = false;
      this.#send({ type: 'mouse', x: mouse.x, y: mouse.y, buttons: mouse.buttons });
    }
    const wheel = this.#wheel;
    if (wheel) {
      this.#wheel = null;
      this.#send({ type: 'wheel', x: wheel.x, y: wheel.y, dx: round1(wheel.dx), dy: round1(wheel.dy) });
    }
  }

  // Keyboard

  /** Give the keyboard to Android. `soft` asks a touch device for its on-screen keyboard. */
  focus(soft = false) {
    const { sink } = this.#o;
    if (!this.capture || (!soft && document.activeElement === sink)) return;
    if (soft && document.activeElement === sink) sink.blur();
    sink.inputMode = soft ? 'text' : 'none';
    sink.focus({ preventScroll: true });
  }

  setCapture(on) {
    this.capture = on;
    if (on) this.focus(true);
    else this.#o.sink.blur();
    this.#o.onCapture(on);
  }

  #keydown(e) {
    if (!this.capture || !this.#o.enabled()) return;
    // The way out for a keyboard user: Tab and Escape go to Android.
    if (e.ctrlKey && e.altKey && e.code === 'KeyK') {
      e.preventDefault();
      this.setCapture(false);
      return;
    }
    this.#deliver(this.#keys.keydown(e), e);
  }

  #keyup(e) {
    if (this.capture && this.#o.enabled()) this.#deliver(this.#keys.keyup(e), e);
  }

  #deliver({ handled, messages }, event) {
    if (handled) event.preventDefault();
    messages.forEach(message => this.#send(message));
  }

  #beforeInput(e) {
    if (!this.capture) return;
    const message = inputMessage(e);
    if (!message) return;
    e.preventDefault();
    this.#send(message);
  }

  #text(text) {
    if (text) chunkText(text).forEach(chunk => this.#send({ type: 'text', text: chunk }));
    this.#o.sink.value = '';
  }

  #paste(e) {
    e.preventDefault();
    this.#text(e.clipboardData?.getData('text/plain'));
  }

  #releaseKeys() {
    this.#keys.releaseAll().forEach(message => this.#send(message));
  }

  /** Lift every finger, button and key: the focus or the connection went away. */
  releaseAll() {
    if (this.#frame) cancelAnimationFrame(this.#frame);
    this.#frame = 0;
    this.#wheel = null;
    this.#touchesMoved = false;
    const lift = this.#touches.releaseAll();
    if (lift) this.#send(lift);
    if (this.#mouse) {
      const { x, y } = this.#mouse;
      this.#mouse = null;
      this.#send({ type: 'mouse', x, y, buttons: 0 });
    }
    this.#releaseKeys();
  }
}
