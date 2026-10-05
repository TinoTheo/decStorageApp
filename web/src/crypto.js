/**
 * dstore browser crypto.
 *
 * Everything here runs on the user's device. The coordinator and the storage
 * nodes only ever receive the outputs of these functions: wrapped keys,
 * encrypted names and encrypted segments.
 *
 * Key hierarchy (details in docs/design.md):
 *
 *   passphrase --PBKDF2-SHA256--> root --HKDF--> auth key   (sent to server, hashed again there)
 *                                       \-HKDF--> KEK        (never leaves the device)
 *   recovery key ---------------------------HKDF--> recovery auth / recovery KEK
 *
 *   master key  (random, wrapped by the KEK and by the recovery KEK)
 *     file key  (random per file, wrapped by the master key)
 *       --HKDF--> segment key  (AES-256-GCM over 4 MB segments)
 *       --HKDF--> name key     (AES-256-GCM over the filename)
 *
 * Uses only WebCrypto, so the same module runs in browsers and in Node 20+.
 */

export const FORMAT_VERSION = 1;
export const NONCE_BYTES = 12;
export const TAG_BYTES = 16;
export const KEY_BYTES = 32;
export const SALT_BYTES = 16;
export const DEFAULT_SEGMENT_SIZE = 4 * 1024 * 1024;
export const DEFAULT_KDF_ITERATIONS = 600_000;
export const KDF_PBKDF2_SHA256 = "pbkdf2-sha256";

const subtle = globalThis.crypto?.subtle;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

const INFO = {
  auth: "dstore/v1/auth",
  kek: "dstore/v1/kek",
  recoveryAuth: "dstore/v1/recovery-auth",
  recoveryKek: "dstore/v1/recovery-kek",
  segment: "dstore/v1/segment-key",
  name: "dstore/v1/name-key",
};

const AAD = {
  master: () => "dstore/v1/master-key",
  fileKey: (fileId) => `dstore/v1/file-key:${fileId}`,
  name: (fileId) => `dstore/v1/name:${fileId}`,
  segment: (fileId) => `dstore/v1/segment:${fileId}`,
};

/** Thrown whenever data fails authentication: wrong key, tampering, reordering or truncation. */
export class IntegrityError extends Error {
  constructor(message) {
    super(message);
    this.name = "IntegrityError";
  }
}

function requireSubtle() {
  if (!subtle) {
    throw new Error("WebCrypto is unavailable. The page must be served over HTTPS (or localhost).");
  }
  return subtle;
}

// ---------------------------------------------------------------------------
// Encoding helpers

export function utf8(text) {
  return encoder.encode(text);
}

export function b64encode(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < view.length; i += chunk) {
    binary += String.fromCharCode(...view.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export function b64decode(text) {
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

export function hex(bytes) {
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function concatBytes(...parts) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

export function randomBytes(length) {
  // getRandomValues refuses more than 65,536 bytes per call.
  const out = new Uint8Array(length);
  for (let offset = 0; offset < length; offset += 65_536) {
    globalThis.crypto.getRandomValues(out.subarray(offset, Math.min(offset + 65_536, length)));
  }
  return out;
}

/** Must match accounts.serializers.normalize_username on the server. */
export function normalizeUsername(username) {
  return String(username).trim().toLowerCase();
}

export async function sha256Hex(bytes) {
  return hex(await requireSubtle().digest("SHA-256", bytes));
}

export function segmentCount(size, segmentSize = DEFAULT_SEGMENT_SIZE) {
  return Math.max(1, Math.ceil(size / segmentSize));
}

export function newFileId() {
  return globalThis.crypto.randomUUID();
}

// ---------------------------------------------------------------------------
// Key derivation

async function hkdfBase(secretBytes) {
  return requireSubtle().importKey("raw", secretBytes, "HKDF", false, ["deriveBits", "deriveKey"]);
}

function hkdfParams(info) {
  return { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32), info: utf8(info) };
}

async function hkdfBits(base, info) {
  return new Uint8Array(await requireSubtle().deriveBits(hkdfParams(info), base, KEY_BYTES * 8));
}

async function hkdfAesKey(base, info) {
  return requireSubtle().deriveKey(hkdfParams(info), base, { name: "AES-GCM", length: 256 }, false, [
    "encrypt",
    "decrypt",
  ]);
}

/**
 * Stretch the passphrase and split it into an auth key (for the server) and a
 * key-encryption key (stays here). NFKC normalisation means the same passphrase
 * typed on different keyboards produces the same keys.
 */
export async function deriveFromPassphrase(passphrase, salt, iterations) {
  if (!passphrase) throw new Error("Passphrase is required.");
  const s = requireSubtle();
  const material = await s.importKey("raw", utf8(passphrase.normalize("NFKC")), "PBKDF2", false, ["deriveBits"]);
  const root = new Uint8Array(
    await s.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, material, KEY_BYTES * 8),
  );
  const base = await hkdfBase(root);
  root.fill(0);
  return {
    authKey: await hkdfBits(base, INFO.auth),
    kek: await hkdfAesKey(base, INFO.kek),
  };
}

// ---------------------------------------------------------------------------
// Recovery key: 32 random bytes shown once as 13 groups of 4 base32 characters.

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function generateRecoveryKey() {
  const bytes = randomBytes(KEY_BYTES);
  return { bytes, display: formatRecoveryKey(bytes) };
}

export function formatRecoveryKey(bytes) {
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += BASE32[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += BASE32[(value << (5 - bits)) & 31];
  return out.match(/.{1,4}/g).join("-");
}

export function parseRecoveryKey(display) {
  const clean = String(display).toUpperCase().replace(/[\s-]/g, "").replace(/0/g, "O").replace(/1/g, "I");
  if (clean.length !== 52 || /[^A-Z2-7]/.test(clean)) {
    throw new Error("That doesn't look like a recovery key. It should be 52 letters and numbers.");
  }
  let bits = 0;
  let value = 0;
  const out = [];
  for (const char of clean) {
    value = (value << 5) | BASE32.indexOf(char);
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(out.slice(0, KEY_BYTES));
}

export async function deriveFromRecoveryKey(recoveryBytes) {
  const base = await hkdfBase(recoveryBytes);
  return {
    recoveryAuth: await hkdfBits(base, INFO.recoveryAuth),
    recoveryKek: await hkdfAesKey(base, INFO.recoveryKek),
  };
}

// ---------------------------------------------------------------------------
// Sealing: version byte + random nonce + AES-GCM ciphertext and tag

async function seal(key, plaintext, aad) {
  const nonce = randomBytes(NONCE_BYTES);
  const ciphertext = new Uint8Array(
    await requireSubtle().encrypt({ name: "AES-GCM", iv: nonce, additionalData: utf8(aad) }, key, plaintext),
  );
  return b64encode(concatBytes(Uint8Array.of(FORMAT_VERSION), nonce, ciphertext));
}

async function open(key, sealed, aad, what) {
  let bytes;
  try {
    bytes = b64decode(sealed);
  } catch {
    throw new IntegrityError(`${what} is not valid base64.`);
  }
  if (bytes.length < 1 + NONCE_BYTES + TAG_BYTES || bytes[0] !== FORMAT_VERSION) {
    throw new IntegrityError(`${what} has an unknown format.`);
  }
  try {
    const plaintext = await requireSubtle().decrypt(
      { name: "AES-GCM", iv: bytes.subarray(1, 1 + NONCE_BYTES), additionalData: utf8(aad) },
      key,
      bytes.subarray(1 + NONCE_BYTES),
    );
    return new Uint8Array(plaintext);
  } catch {
    throw new IntegrityError(`${what} failed to decrypt: wrong key or tampered data.`);
  }
}

// ---------------------------------------------------------------------------
// Master key

export function generateMasterKeyBytes() {
  return randomBytes(KEY_BYTES);
}

export async function importMasterKey(rawBytes) {
  return requireSubtle().importKey("raw", rawBytes, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

export function wrapMasterKey(rawMasterKey, wrappingKey) {
  return seal(wrappingKey, rawMasterKey, AAD.master());
}

/** Returns a non-extractable CryptoKey. The raw bytes are wiped after import. */
export async function unwrapMasterKey(wrapped, wrappingKey) {
  const raw = await open(wrappingKey, wrapped, AAD.master(), "Master key");
  if (raw.length !== KEY_BYTES) throw new IntegrityError("Master key has the wrong length.");
  try {
    return await importMasterKey(raw);
  } finally {
    raw.fill(0);
  }
}

/** Unwraps to raw bytes; only used when re-wrapping during recovery. Wipe after use. */
export async function unwrapMasterKeyBytes(wrapped, wrappingKey) {
  const raw = await open(wrappingKey, wrapped, AAD.master(), "Master key");
  if (raw.length !== KEY_BYTES) throw new IntegrityError("Master key has the wrong length.");
  return raw;
}

// ---------------------------------------------------------------------------
// Files

export function generateFileKeyBytes() {
  return randomBytes(KEY_BYTES);
}

export function wrapFileKey(rawFileKey, masterKey, fileId) {
  return seal(masterKey, rawFileKey, AAD.fileKey(fileId));
}

export async function unwrapFileKey(wrapped, masterKey, fileId) {
  const raw = await open(masterKey, wrapped, AAD.fileKey(fileId), "File key");
  if (raw.length !== KEY_BYTES) throw new IntegrityError("File key has the wrong length.");
  return raw;
}

/** Splits one file key into independent keys for segments and for the name. */
export async function deriveFileKeys(rawFileKey) {
  const base = await hkdfBase(rawFileKey);
  return {
    segmentKey: await hkdfAesKey(base, INFO.segment),
    nameKey: await hkdfAesKey(base, INFO.name),
  };
}

export function encryptName(name, nameKey, fileId) {
  return seal(nameKey, utf8(name), AAD.name(fileId));
}

export async function decryptName(sealed, nameKey, fileId) {
  const bytes = await open(nameKey, sealed, AAD.name(fileId), "Filename");
  try {
    return decoder.decode(bytes);
  } catch {
    throw new IntegrityError("Filename is not valid UTF-8.");
  }
}

// ---------------------------------------------------------------------------
// Segments
//
// Nonce: 4 zero bytes + 64-bit big-endian segment index. Safe because every file
// has its own segment key, so (key, nonce) never repeats.
//
// AAD binds the file ID, the segment index and whether this is the last segment.
// Reordering segments, moving one into another file, or dropping the tail of a
// file all make decryption fail instead of silently returning wrong data.

export function segmentNonce(index) {
  const nonce = new Uint8Array(NONCE_BYTES);
  new DataView(nonce.buffer).setBigUint64(4, BigInt(index));
  return nonce;
}

export function segmentAad(fileId, index, isLast) {
  const tail = new Uint8Array(5);
  const view = new DataView(tail.buffer);
  view.setUint32(0, index);
  view.setUint8(4, isLast ? 1 : 0);
  return concatBytes(utf8(AAD.segment(fileId)), tail);
}

export async function encryptSegment(segmentKey, fileId, index, isLast, plaintext) {
  const ciphertext = await requireSubtle().encrypt(
    { name: "AES-GCM", iv: segmentNonce(index), additionalData: segmentAad(fileId, index, isLast) },
    segmentKey,
    plaintext,
  );
  return new Uint8Array(ciphertext);
}

export async function decryptSegment(segmentKey, fileId, index, isLast, ciphertext) {
  try {
    const plaintext = await requireSubtle().decrypt(
      { name: "AES-GCM", iv: segmentNonce(index), additionalData: segmentAad(fileId, index, isLast) },
      segmentKey,
      ciphertext,
    );
    return new Uint8Array(plaintext);
  } catch {
    throw new IntegrityError(
      `Segment ${index} failed to decrypt: it was altered, reordered, or the file was cut short.`,
    );
  }
}
