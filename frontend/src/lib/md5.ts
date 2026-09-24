/**
 * Compact MD5 (RFC 1321) implementation, dependency-free.
 *
 * Used only to derive the MD5 hex digest of the Vereinsflieger password
 * client-side, because the VF REST API expects the password as MD5 hash
 * and the backend must never see the plaintext ("store what you send").
 * Not a cryptographic primitive for anything else.
 */

const S = [
  7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
  5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
  4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
  6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
]

const K = Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) >>> 0)

function rotl(x: number, c: number): number {
  return ((x << c) | (x >>> (32 - c))) >>> 0
}

function md5Bytes(input: Uint8Array): Uint8Array {
  const bitLen = input.length * 8
  // Padding: 0x80, zeros, 64-bit little-endian length → multiple of 64 bytes
  const padLen = (((input.length + 8) >>> 6) + 1) << 6
  const buf = new Uint8Array(padLen)
  buf.set(input)
  buf[input.length] = 0x80
  const view = new DataView(buf.buffer)
  view.setUint32(padLen - 8, bitLen >>> 0, true)
  view.setUint32(padLen - 4, Math.floor(bitLen / 2 ** 32) >>> 0, true)

  let a0 = 0x67452301
  let b0 = 0xefcdab89
  let c0 = 0x98badcfe
  let d0 = 0x10325476

  const M = new Uint32Array(16)
  for (let off = 0; off < padLen; off += 64) {
    for (let i = 0; i < 16; i++) M[i] = view.getUint32(off + i * 4, true)

    let A = a0
    let B = b0
    let C = c0
    let D = d0

    for (let i = 0; i < 64; i++) {
      let F: number
      let g: number
      if (i < 16) {
        F = (B & C) | (~B & D)
        g = i
      } else if (i < 32) {
        F = (D & B) | (~D & C)
        g = (5 * i + 1) % 16
      } else if (i < 48) {
        F = B ^ C ^ D
        g = (3 * i + 5) % 16
      } else {
        F = C ^ (B | ~D)
        g = (7 * i) % 16
      }
      const tmp = D
      D = C
      C = B
      const sum = (A + F + (K[i] ?? 0) + (M[g] ?? 0)) >>> 0
      B = (B + rotl(sum, S[i] ?? 0)) >>> 0
      A = tmp
    }

    a0 = (a0 + A) >>> 0
    b0 = (b0 + B) >>> 0
    c0 = (c0 + C) >>> 0
    d0 = (d0 + D) >>> 0
  }

  const out = new Uint8Array(16)
  const ov = new DataView(out.buffer)
  ov.setUint32(0, a0, true)
  ov.setUint32(4, b0, true)
  ov.setUint32(8, c0, true)
  ov.setUint32(12, d0, true)
  return out
}

/** MD5 hex digest (lowercase) of a UTF-8 string. */
export function md5Hex(text: string): string {
  const bytes = new TextEncoder().encode(text)
  return Array.from(md5Bytes(bytes), (b) => b.toString(16).padStart(2, '0')).join('')
}
