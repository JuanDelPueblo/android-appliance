// A minimal TURN client (RFC 8656, long-term credentials) that asks the
// appliance's TURN server for a relay and for permissions to peers. It shows
// what the server refuses without a browser: the relay must reach the
// container's own address and nothing else.
//
// probe({host, port, username, credential, transport, peers}) resolves to
//   {allocate, relayed, permissions: {<peer>: <status>}}
// where each status is the STUN response: 200 for success, else the error code.

import crypto from 'node:crypto';
import dgram from 'node:dgram';
import net from 'node:net';

const COOKIE = 0x2112a442;
const TYPE = { allocate: 0x0003, createPermission: 0x0008, refresh: 0x0004 };
const ATTR = {
  username: 0x0006,
  integrity: 0x0008,
  errorCode: 0x0009,
  realm: 0x0014,
  nonce: 0x0015,
  xorPeer: 0x0012,
  xorRelayed: 0x0016,
  transport: 0x0019,
  lifetime: 0x000d,
};

const pad = length => (4 - (length % 4)) % 4;

function attribute(type, value) {
  const head = Buffer.alloc(4);
  head.writeUInt16BE(type, 0);
  head.writeUInt16BE(value.length, 2);
  return Buffer.concat([head, value, Buffer.alloc(pad(value.length))]);
}

function xorAddress(address, port) {
  const value = Buffer.alloc(8);
  value.writeUInt8(1, 1);
  value.writeUInt16BE(port ^ (COOKIE >>> 16), 2);
  const octets = address.split('.').map(Number);
  value.writeUInt32BE((((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) ^ COOKIE) >>> 0, 4);
  return value;
}

function parseXorAddress(value) {
  const raw = (value.readUInt32BE(4) ^ COOKIE) >>> 0;
  return `${raw >>> 24}.${(raw >>> 16) & 255}.${(raw >>> 8) & 255}.${raw & 255}`;
}

function message(type, transaction, attributes, key) {
  let body = Buffer.concat(attributes);
  const head = length => {
    const h = Buffer.alloc(20);
    h.writeUInt16BE(type, 0);
    h.writeUInt16BE(length, 2);
    h.writeUInt32BE(COOKIE, 4);
    transaction.copy(h, 8);
    return h;
  };
  if (key) {
    // The integrity covers the message as if its length ended after itself.
    const mac = crypto.createHmac('sha1', key).update(Buffer.concat([head(body.length + 24), body])).digest();
    body = Buffer.concat([body, attribute(ATTR.integrity, mac)]);
  }
  return Buffer.concat([head(body.length), body]);
}

function parse(buffer) {
  const type = buffer.readUInt16BE(0);
  const attributes = new Map();
  for (let at = 20; at + 4 <= buffer.length; ) {
    const length = buffer.readUInt16BE(at + 2);
    attributes.set(buffer.readUInt16BE(at), buffer.subarray(at + 4, at + 4 + length));
    at += 4 + length + pad(length);
  }
  const error = attributes.get(ATTR.errorCode);
  // An error response carries ERROR-CODE (class, number); anything else is a success.
  return { type, attributes, status: error ? error.readUInt8(2) * 100 + error.readUInt8(3) : 200 };
}

// One request, one response, over UDP or over a TCP stream.
async function connect(host, port, transport) {
  if (transport === 'udp') {
    const socket = dgram.createSocket('udp4');
    let waiting = null;
    socket.on('message', data => waiting?.(data));
    return {
      request: (data, ms = 4000) =>
        new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('no answer from the TURN server')), ms);
          waiting = data => {
            clearTimeout(timer);
            resolve(data);
          };
          socket.send(data, port, host);
        }),
      close: () => socket.close(),
    };
  }
  const socket = net.connect(port, host);
  await new Promise((resolve, reject) => socket.once('connect', resolve).once('error', reject));
  let pending = Buffer.alloc(0);
  let waiting = null;
  socket.on('data', chunk => {
    pending = Buffer.concat([pending, chunk]);
    if (pending.length >= 20 && pending.length >= 20 + pending.readUInt16BE(2)) {
      const whole = pending.subarray(0, 20 + pending.readUInt16BE(2));
      pending = pending.subarray(whole.length);
      waiting?.(whole);
    }
  });
  return {
    request: (data, ms = 4000) =>
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('no answer from the TURN server')), ms);
        waiting = data => {
          clearTimeout(timer);
          resolve(data);
        };
        socket.write(data);
      }),
    close: () => socket.destroy(),
  };
}

export async function probe({ host, port, username, credential, transport = 'udp', peers = [] }) {
  const client = await connect(host, port, transport);
  try {
    const transaction = () => crypto.randomBytes(12);
    const udp = attribute(ATTR.transport, Buffer.from([17, 0, 0, 0]));
    // The first request has no credentials: the server answers 401 with its realm and nonce.
    const first = parse(await client.request(message(TYPE.allocate, transaction(), [udp])));
    if (first.status !== 401) return { allocate: first.status, relayed: null, permissions: {} };
    let realm = first.attributes.get(ATTR.realm);
    let nonce = first.attributes.get(ATTR.nonce);
    const key = () => crypto.createHash('md5').update(`${username}:${realm}:${credential}`).digest();
    const signed = (type, extra) =>
      message(type, transaction(), [...extra, attribute(ATTR.username, Buffer.from(username)), attribute(ATTR.realm, realm), attribute(ATTR.nonce, nonce)], key());

    const allocation = parse(await client.request(signed(TYPE.allocate, [udp])));
    const result = { allocate: allocation.status, relayed: null, permissions: {} };
    if (allocation.status !== 200) return result;
    result.relayed = parseXorAddress(allocation.attributes.get(ATTR.xorRelayed));
    for (const peer of peers) {
      const reply = parse(await client.request(signed(TYPE.createPermission, [attribute(ATTR.xorPeer, xorAddress(peer, 9))])));
      // A stale nonce asks for a retry with the new one.
      if (reply.status === 438) {
        nonce = reply.attributes.get(ATTR.nonce);
        result.permissions[peer] = parse(await client.request(signed(TYPE.createPermission, [attribute(ATTR.xorPeer, xorAddress(peer, 9))]))).status;
      } else {
        result.permissions[peer] = reply.status;
      }
    }
    // Free the relay at once.
    await client.request(signed(TYPE.refresh, [attribute(ATTR.lifetime, Buffer.alloc(4))])).catch(() => {});
    return result;
  } finally {
    client.close();
  }
}
