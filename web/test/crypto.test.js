import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  IntegrityError,
  b64decode,
  b64encode,
  decryptName,
  decryptSegment,
  deriveFileKeys,
  deriveFromPassphrase,
  deriveFromRecoveryKey,
  encryptName,
  encryptSegment,
  formatRecoveryKey,
  generateFileKeyBytes,
  generateMasterKeyBytes,
  generateRecoveryKey,
  importMasterKey,
  newFileId,
  normalizeUsername,
  parseRecoveryKey,
  randomBytes,
  segmentCount,
  segmentNonce,
  sha256Hex,
  unwrapFileKey,
  unwrapMasterKey,
  unwrapMasterKeyBytes,
  wrapFileKey,
  wrapMasterKey,
} from "../src/crypto.js";

// Low iteration count keeps unit tests fast; the server enforces the real minimum.
const FAST_ITERATIONS = 1_000;

async function fileFixture() {
  const fileId = newFileId();
  const raw = generateFileKeyBytes();
  return { fileId, raw, ...(await deriveFileKeys(raw)) };
}

describe("encoding", () => {
  it("round-trips base64 including large buffers", () => {
    const bytes = randomBytes(200_000);
    assert.deepEqual(b64decode(b64encode(bytes)), bytes);
  });

  it("normalises usernames the same way as the server", () => {
    assert.equal(normalizeUsername("  Thandi.M  "), "thandi.m");
  });

  it("counts segments with an empty file as one segment", () => {
    const size = 4 * 1024 * 1024;
    assert.equal(segmentCount(0, size), 1);
    assert.equal(segmentCount(1, size), 1);
    assert.equal(segmentCount(size, size), 1);
    assert.equal(segmentCount(size + 1, size), 2);
  });

  it("builds unique 12-byte nonces from the segment index", () => {
    assert.equal(segmentNonce(0).length, 12);
    assert.notDeepEqual(segmentNonce(1), segmentNonce(2));
    assert.deepEqual([...segmentNonce(258).slice(-2)], [1, 2]);
  });
});

describe("passphrase derivation", () => {
  const salt = randomBytes(16);

  it("is deterministic for the same passphrase and salt", async () => {
    const a = await deriveFromPassphrase("correct horse battery staple", salt, FAST_ITERATIONS);
    const b = await deriveFromPassphrase("correct horse battery staple", salt, FAST_ITERATIONS);
    assert.deepEqual(a.authKey, b.authKey);
  });

  it("treats visually identical Unicode passphrases as the same", async () => {
    const composed = await deriveFromPassphrase("café pass", salt, FAST_ITERATIONS);
    const decomposed = await deriveFromPassphrase("café pass", salt, FAST_ITERATIONS);
    assert.deepEqual(composed.authKey, decomposed.authKey);
  });

  it("keeps the auth key unrelated to the key-encryption key", async () => {
    const { authKey, kek } = await deriveFromPassphrase("passphrase one", salt, FAST_ITERATIONS);
    const master = generateMasterKeyBytes();
    const wrapped = await wrapMasterKey(master, kek);
    // Someone holding only the auth key (the server) can't use it to unwrap.
    const authAsKey = await importMasterKey(authKey);
    await assert.rejects(unwrapMasterKey(wrapped, authAsKey), IntegrityError);
  });

  it("rejects an empty passphrase", async () => {
    await assert.rejects(deriveFromPassphrase("", salt, FAST_ITERATIONS));
  });
});

describe("master key wrapping", () => {
  it("unwraps with the right passphrase and fails with the wrong one", async () => {
    const salt = randomBytes(16);
    const right = await deriveFromPassphrase("right passphrase", salt, FAST_ITERATIONS);
    const wrong = await deriveFromPassphrase("wrong passphrase", salt, FAST_ITERATIONS);
    const master = generateMasterKeyBytes();
    const wrapped = await wrapMasterKey(master, right.kek);

    assert.equal(b64decode(wrapped).length, 61, "must match WRAPPED_KEY_BYTES on the server");
    assert.deepEqual(await unwrapMasterKeyBytes(wrapped, right.kek), master);
    await assert.rejects(unwrapMasterKey(wrapped, wrong.kek), IntegrityError);
  });

  it("returns a master key that can't be exported", async () => {
    const { kek } = await deriveFromPassphrase("x passphrase", randomBytes(16), FAST_ITERATIONS);
    const key = await unwrapMasterKey(await wrapMasterKey(generateMasterKeyBytes(), kek), kek);
    assert.equal(key.extractable, false);
  });

  it("detects a single flipped bit", async () => {
    const { kek } = await deriveFromPassphrase("x passphrase", randomBytes(16), FAST_ITERATIONS);
    const bytes = b64decode(await wrapMasterKey(generateMasterKeyBytes(), kek));
    bytes[30] ^= 1;
    await assert.rejects(unwrapMasterKey(b64encode(bytes), kek), IntegrityError);
  });
});

describe("recovery key", () => {
  it("formats as 13 groups of 4 and parses back", () => {
    const { bytes, display } = generateRecoveryKey();
    assert.match(display, /^([A-Z2-7]{4}-){12}[A-Z2-7]{4}$/);
    assert.deepEqual(parseRecoveryKey(display), bytes);
  });

  it("tolerates lowercase, spaces and 0/1 typed for O/I", () => {
    const bytes = new Uint8Array(32).fill(0b01110011);
    const display = formatRecoveryKey(bytes);
    const sloppy = display.toLowerCase().replace(/-/g, " ").replace(/o/g, "0").replace(/i/g, "1");
    assert.deepEqual(parseRecoveryKey(sloppy), bytes);
  });

  it("rejects malformed keys", () => {
    assert.throws(() => parseRecoveryKey("ABCD-EFGH"));
    assert.throws(() => parseRecoveryKey("8".repeat(52)));
  });

  it("unwraps the same master key as the passphrase does", async () => {
    const master = generateMasterKeyBytes();
    const { kek } = await deriveFromPassphrase("my passphrase", randomBytes(16), FAST_ITERATIONS);
    const recovery = generateRecoveryKey();
    const { recoveryKek } = await deriveFromRecoveryKey(recovery.bytes);

    const viaPassphrase = await unwrapMasterKeyBytes(await wrapMasterKey(master, kek), kek);
    const viaRecovery = await unwrapMasterKeyBytes(await wrapMasterKey(master, recoveryKek), recoveryKek);
    assert.deepEqual(viaPassphrase, master);
    assert.deepEqual(viaRecovery, master);
  });
});

describe("file keys and names", () => {
  it("binds the wrapped file key to its file ID", async () => {
    const master = await importMasterKey(generateMasterKeyBytes());
    const raw = generateFileKeyBytes();
    const fileId = newFileId();
    const wrapped = await wrapFileKey(raw, master, fileId);

    assert.deepEqual(await unwrapFileKey(wrapped, master, fileId), raw);
    // A server swapping keys between files is caught.
    await assert.rejects(unwrapFileKey(wrapped, master, newFileId()), IntegrityError);
  });

  it("encrypts names and binds them to the file", async () => {
    const { fileId, nameKey } = await fileFixture();
    const sealed = await encryptName("Ndebele notes – 2026.pdf", nameKey, fileId);
    assert.equal(await decryptName(sealed, nameKey, fileId), "Ndebele notes – 2026.pdf");
    await assert.rejects(decryptName(sealed, nameKey, newFileId()), IntegrityError);
  });

  it("produces different ciphertext for the same name each time", async () => {
    const { fileId, nameKey } = await fileFixture();
    assert.notEqual(await encryptName("a.txt", nameKey, fileId), await encryptName("a.txt", nameKey, fileId));
  });
});

describe("segments", () => {
  it("round-trips and adds exactly a 16-byte tag", async () => {
    const { fileId, segmentKey } = await fileFixture();
    const plaintext = randomBytes(1000);
    const ciphertext = await encryptSegment(segmentKey, fileId, 0, true, plaintext);
    assert.equal(ciphertext.length, 1016);
    assert.deepEqual(await decryptSegment(segmentKey, fileId, 0, true, ciphertext), plaintext);
  });

  it("handles an empty last segment (empty file)", async () => {
    const { fileId, segmentKey } = await fileFixture();
    const ciphertext = await encryptSegment(segmentKey, fileId, 0, true, new Uint8Array(0));
    assert.equal(ciphertext.length, 16);
    assert.equal((await decryptSegment(segmentKey, fileId, 0, true, ciphertext)).length, 0);
  });

  it("detects tampering", async () => {
    const { fileId, segmentKey } = await fileFixture();
    const ciphertext = await encryptSegment(segmentKey, fileId, 0, true, randomBytes(64));
    ciphertext[10] ^= 0xff;
    await assert.rejects(decryptSegment(segmentKey, fileId, 0, true, ciphertext), IntegrityError);
  });

  it("detects segments swapped into the wrong position", async () => {
    const { fileId, segmentKey } = await fileFixture();
    const first = await encryptSegment(segmentKey, fileId, 0, false, randomBytes(64));
    await assert.rejects(decryptSegment(segmentKey, fileId, 1, false, first), IntegrityError);
  });

  it("detects a file cut short", async () => {
    // A 3-segment file whose last segment is withheld: segment 1 was not
    // encrypted as "last", so presenting it as the final segment fails.
    const { fileId, segmentKey } = await fileFixture();
    const middle = await encryptSegment(segmentKey, fileId, 1, false, randomBytes(64));
    await assert.rejects(decryptSegment(segmentKey, fileId, 1, true, middle), IntegrityError);
  });

  it("detects a segment moved from another file", async () => {
    const a = await fileFixture();
    const ciphertext = await encryptSegment(a.segmentKey, a.fileId, 0, true, randomBytes(64));
    await assert.rejects(decryptSegment(a.segmentKey, newFileId(), 0, true, ciphertext), IntegrityError);
  });

  it("hashes with SHA-256", async () => {
    assert.equal(
      await sha256Hex(new TextEncoder().encode("abc")),
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });
});
