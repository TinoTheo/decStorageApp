#!/usr/bin/env node
/**
 * End-to-end test against a running coordinator, using the same client library
 * the browser uses. Run it with `python tasks.py e2e`, which starts a throwaway
 * server, or point it at one yourself:
 *
 *   BASE_URL=http://127.0.0.1:8000 STAGING_DIR=coordinator/staging node scripts/e2e.mjs
 *
 * STAGING_DIR lets the test inspect the bytes the server actually stored.
 */

import assert from "node:assert/strict";
import { readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { ApiError, DStoreClient, IntegrityError } from "../web/src/client.js";
import { randomBytes } from "../web/src/crypto.js";

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8765";
const STAGING_DIR = process.env.STAGING_DIR;
const SEGMENT = 4 * 1024 * 1024;
const MARKER = new TextEncoder().encode("PLAINTEXT-MARKER-THAT-MUST-NEVER-REACH-THE-SERVER");

let step = 0;
async function check(title, fn) {
  step++;
  const started = Date.now();
  await fn();
  console.log(`  ok ${String(step).padStart(2)}  ${title}  (${Date.now() - started} ms)`);
}

const newClient = () => new DStoreClient({ baseUrl: BASE_URL, retries: 2 });

/** 2.4 segments of data, with a recognisable marker every 64 KB. */
function samplePlaintext() {
  const size = 2 * SEGMENT + Math.floor(SEGMENT * 0.4);
  const bytes = randomBytes(size);
  for (let offset = 0; offset + MARKER.length < size; offset += 64 * 1024) bytes.set(MARKER, offset);
  return bytes;
}

function contains(haystack, needle) {
  outer: for (let i = 0; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) if (haystack[i + j] !== needle[j]) continue outer;
    return true;
  }
  return false;
}

async function stagedFiles() {
  if (!STAGING_DIR) return [];
  const out = [];
  async function walk(dir) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) await walk(full);
      else out.push(full);
    }
  }
  await walk(STAGING_DIR).catch(() => {});
  return out;
}

async function expectApiError(promise, status) {
  await assert.rejects(promise, (err) => err instanceof ApiError && err.status === status);
}

async function main() {
  console.log(`dstore end-to-end against ${BASE_URL}`);
  const health = await fetch(`${BASE_URL}/health`).then((r) => r.json());
  assert.equal(health.status, "ok");

  const username = `e2e-${Date.now().toString(36)}`;
  const passphrase = "copper lantern quietly folds the map";
  const plaintext = samplePlaintext();
  const blob = new Blob([plaintext]);
  const alice = newClient();
  let recoveryKey;
  let fileId;

  await check("register creates keys in the client and returns a recovery key", async () => {
    ({ recoveryKey } = await alice.register(username, passphrase));
    assert.match(recoveryKey, /^([A-Z2-7]{4}-){12}[A-Z2-7]{4}$/);
    assert.ok(alice.isSignedIn);
  });

  await check("an interrupted upload resumes without resending finished segments", async () => {
    const controller = new AbortController();
    await assert.rejects(
      alice.uploadFile(blob, {
        name: "field-report.pdf",
        concurrency: 1,
        signal: controller.signal,
        onCreated: (id) => (fileId = id),
        onProgress: ({ sentBytes }) => sentBytes > 0 && controller.abort(),
      }),
    );
    const partial = (await alice.listFiles()).find((f) => f.id === fileId);
    assert.equal(partial.status, "uploading");

    let firstReport;
    const manifest = await alice.resumeUpload(fileId, blob, {
      onProgress: ({ sentBytes }) => (firstReport ??= sentBytes),
    });
    assert.equal(firstReport, SEGMENT, "resume should start after the segment that already landed");
    assert.equal(manifest.status, "complete");
    assert.equal(manifest.segment_count, 3);
    assert.equal(manifest.size, plaintext.length);
  });

  await check("the file list decrypts names locally", async () => {
    const files = await alice.listFiles();
    assert.deepEqual(
      files.map((f) => [f.name, f.size, f.status]),
      [["field-report.pdf", plaintext.length, "complete"]],
    );
  });

  await check("download verifies and decrypts back to the original bytes", async () => {
    const result = await alice.downloadFile(fileId);
    assert.equal(result.name, "field-report.pdf");
    assert.deepEqual(new Uint8Array(await result.blob.arrayBuffer()), plaintext);
  });

  if (STAGING_DIR) {
    await check("the server only ever stored ciphertext", async () => {
      const files = await stagedFiles();
      assert.equal(files.length, 3);
      let total = 0;
      for (const file of files) {
        const bytes = await readFile(file);
        total += bytes.length;
        assert.ok(!contains(bytes, MARKER), `${path.basename(file)} contains plaintext`);
        assert.ok(!contains(bytes, new TextEncoder().encode("field-report")), "filename leaked");
      }
      assert.equal(total, plaintext.length + 3 * 16, "ciphertext = plaintext + one 16-byte tag per segment");
    });
  }

  await check("a wrong passphrase is refused and an unknown user looks the same", async () => {
    const eve = newClient();
    await expectApiError(eve.login(username, "not the passphrase"), 401);
    await expectApiError(eve.login(`${username}-nobody`, "not the passphrase"), 401);
  });

  await check("signing in again on a new device restores access", async () => {
    const laptop = newClient();
    await laptop.login(username.toUpperCase(), passphrase);
    const result = await laptop.downloadFile(fileId);
    assert.deepEqual(new Uint8Array(await result.blob.arrayBuffer()), plaintext);
    await laptop.logout();
  });

  await check("another account can't see or fetch the file", async () => {
    const bob = newClient();
    await bob.register(`${username}-bob`, "a completely different passphrase");
    assert.deepEqual(await bob.listFiles(), []);
    await expectApiError(bob.downloadFile(fileId), 404);
  });

  if (STAGING_DIR) {
    await check("a segment altered at rest is caught, not returned", async () => {
      const [victim] = await stagedFiles();
      const original = await readFile(victim);
      const altered = Buffer.from(original);
      altered[100] ^= 0x01;
      await writeFile(victim, altered);
      try {
        // The server checks every segment's SHA-256 before sending it, so the
        // damaged copy never leaves the server (503); if it ever did, the
        // browser's own checks would raise IntegrityError instead.
        await assert.rejects(
          alice.downloadFile(fileId),
          (err) => (err instanceof ApiError && err.status === 503) || err instanceof IntegrityError,
        );
      } finally {
        await writeFile(victim, original);
      }
      await alice.downloadFile(fileId); // healthy again once restored
    });
  }

  await check("the recovery key sets a new passphrase and keeps every file readable", async () => {
    const phone = newClient();
    await phone.login(username, passphrase);
    const newPassphrase = "seven green buses wait by the river";
    const rescued = newClient();
    await rescued.recover(username, recoveryKey.toLowerCase(), newPassphrase);

    const result = await rescued.downloadFile(fileId);
    assert.deepEqual(new Uint8Array(await result.blob.arrayBuffer()), plaintext);
    await expectApiError(phone.listFiles(), 401); // other sessions were signed out
    await expectApiError(newClient().login(username, passphrase), 401);

    const fresh = newClient();
    await fresh.login(username, newPassphrase);
    assert.equal((await fresh.listFiles()).length, 1);
    Object.assign(alice, { token: fresh.token, masterKey: fresh.masterKey });
  });

  await check("a wrong recovery key is refused", async () => {
    const fake = "AAAA-".repeat(12) + "AAAA";
    await expectApiError(newClient().recover(username, fake, "whatever new passphrase"), 401);
  });

  await check("empty files round-trip", async () => {
    const manifest = await alice.uploadFile(new Blob([]), { name: "empty.txt" });
    const result = await alice.downloadFile(manifest.id);
    assert.equal(result.name, "empty.txt");
    assert.equal(result.blob.size, 0);
    await alice.deleteFile(manifest.id);
  });

  await check("delete removes the file and its stored segments", async () => {
    await alice.deleteFile(fileId);
    assert.deepEqual(await alice.listFiles(), []);
    if (STAGING_DIR) assert.equal((await stagedFiles()).length, 0);
  });

  console.log(`\nAll ${step} checks passed.`);
}

main().catch((err) => {
  console.error("\nEnd-to-end test FAILED:\n", err);
  process.exit(1);
});
