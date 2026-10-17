// The fingers on the display.
//
// The daemon maps these ids to emulator ids, and the emulator takes at most
// ten contacts. Every message lists all contacts that are down, the way the
// emulator takes a gesture; a lifted contact is listed once more with
// pressure 0.

export const MAX_CONTACTS = 10;

/** PointerEvent.pressure, as the daemon takes it: above 0 up to 1 (0 lifts). No pressure is a full press. */
export function touchPressure(pressure) {
  return pressure > 0 ? Math.min(1, pressure) : 1;
}

export class TouchTable {
  #contacts = new Map();

  has(pointerId) {
    return this.#contacts.has(pointerId);
  }

  get size() {
    return this.#contacts.size;
  }

  // The lowest id that no contact uses. Ids start at 1: 0 can read as "none".
  #freeId() {
    const used = new Set([...this.#contacts.values()].map(c => c.id));
    for (let id = 1; id <= MAX_CONTACTS; id++) if (!used.has(id)) return id;
    return null;
  }

  #message(lifted) {
    const touches = [...this.#contacts.values()].map(({ id, x, y, pressure }) => ({ id, x, y, pressure }));
    if (lifted) touches.push({ id: lifted.id, x: lifted.x, y: lifted.y, pressure: 0 });
    return { type: 'touch', touches };
  }

  /** Returns the message, or null when ten contacts are down already. */
  down(pointerId, x, y, pressure) {
    const id = this.#freeId();
    if (id === null) return null;
    this.#contacts.set(pointerId, { id, x, y, pressure });
    return this.#message();
  }

  /** Returns false when nothing changed. */
  move(pointerId, x, y, pressure) {
    const contact = this.#contacts.get(pointerId);
    if (!contact || (contact.x === x && contact.y === y && contact.pressure === pressure)) return false;
    Object.assign(contact, { x, y, pressure });
    return true;
  }

  snapshot() {
    return this.#contacts.size ? this.#message() : null;
  }

  up(pointerId, x, y) {
    const contact = this.#contacts.get(pointerId);
    if (!contact) return null;
    this.#contacts.delete(pointerId);
    return this.#message({ ...contact, x: x ?? contact.x, y: y ?? contact.y });
  }

  /** One message that lifts every contact, or null. */
  releaseAll() {
    if (!this.#contacts.size) return null;
    const touches = [...this.#contacts.values()].map(({ id, x, y }) => ({ id, x, y, pressure: 0 }));
    this.#contacts.clear();
    return { type: 'touch', touches };
  }
}
