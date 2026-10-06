/**
 * dstore API client.
 *
 * Wraps the coordinator's HTTP API and does all encryption on the way out and
 * decryption on the way in. Works in browsers and in Node 20+ (tests use it
 * against a live server).
 *
 *   const client = new DStoreClient({ baseUrl: "https://coordinator.example" });
 *   const { recoveryKey } = await client.register("thandi", "a long passphrase");
 *   const manifest = await client.uploadFile(file, { onProgress });
 *   const { name, blob } = await client.downloadFile(manifest.id);
 */

import {
  DEFAULT_KDF_ITERATIONS,
  DEFAULT_SEGMENT_SIZE,
  IntegrityError,
  KDF_PBKDF2_SHA256,
  SALT_BYTES,
  b64decode,
  b64encode,
  decryptName,
  decryptSegment,
  deriveFileKeys,
  deriveFromPassphrase,
  deriveFromRecoveryKey,
  encryptName,
  encryptSegment,
  generateFileKeyBytes,
  generateMasterKeyBytes,
  generateRecoveryKey,
  newFileId,
  normalizeUsername,
  parseRecoveryKey,
  randomBytes,
  segmentCount,
  sha256Hex,
  unwrapFileKey,
  unwrapMasterKey,
  unwrapMasterKeyBytes,
  wrapFileKey,
  wrapMasterKey,
} from "./crypto.js";

export { IntegrityError };

export class ApiError extends Error {
  constructor(status, body) {
    const detail = typeof body === "object" && body ? body.detail || JSON.stringify(body) : String(body || "");
    super(`HTTP ${status}${detail ? `: ${detail}` : ""}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504]);

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason);
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason);
      },
      { once: true },
    );
  });
}

/** Runs `worker(item)` over `items` with at most `limit` in flight. Stops on the first error. */
async function runPool(items, limit, worker, signal) {
  let next = 0;
  let failure = null;
  const lanes = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (failure === null && next < items.length) {
      signal?.throwIfAborted();
      const item = items[next++];
      try {
        await worker(item);
      } catch (error) {
        failure ??= error;
      }
    }
  });
  await Promise.all(lanes);
  if (failure) throw failure;
}

export class DStoreClient {
  /**
   * @param {object} [options]
   * @param {string} [options.baseUrl]  Coordinator origin; empty means same origin.
   * @param {typeof fetch} [options.fetch]
   * @param {number} [options.kdfIterations]  Used for new passphrases only.
   * @param {number} [options.segmentSize]  Must match the server's SEGMENT_SIZE.
   * @param {number} [options.retries]  Attempts per request on network errors and 5xx.
   */
  constructor({
    baseUrl = "",
    fetch: fetchImpl,
    kdfIterations = DEFAULT_KDF_ITERATIONS,
    segmentSize = DEFAULT_SEGMENT_SIZE,
    retries = 4,
  } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.fetch = fetchImpl || globalThis.fetch.bind(globalThis);
    this.kdfIterations = kdfIterations;
    this.segmentSize = segmentSize;
    this.retries = retries;
    this.token = null;
    this.username = null;
    this.masterKey = null;
  }

  get isSignedIn() {
    return Boolean(this.token && this.masterKey);
  }

  // -------------------------------------------------------------------------
  // HTTP

  async _send(method, path, { json, body, headers = {}, signal, expect = "json", retry = true } = {}) {
    const requestHeaders = { ...headers };
    if (this.token) requestHeaders.Authorization = `Token ${this.token}`;
    let payload = body;
    if (json !== undefined) {
      requestHeaders["Content-Type"] = "application/json";
      payload = JSON.stringify(json);
    }

    const attempts = retry ? this.retries : 1;
    for (let attempt = 1; ; attempt++) {
      signal?.throwIfAborted();
      let response;
      try {
        response = await this.fetch(`${this.baseUrl}${path}`, { method, headers: requestHeaders, body: payload, signal });
      } catch (error) {
        if (signal?.aborted || attempt >= attempts) throw error;
        await sleep(this._backoff(attempt), signal);
        continue;
      }

      if (response.ok) {
        if (expect === "none" || response.status === 204) return null;
        if (expect === "response") return response;
        return response.json();
      }

      if (RETRYABLE_STATUS.has(response.status) && attempt < attempts) {
        await response.body?.cancel?.();
        await sleep(this._backoff(attempt), signal);
        continue;
      }

      let errorBody;
      const text = await response.text();
      try {
        errorBody = JSON.parse(text);
      } catch {
        errorBody = text;
      }
      throw new ApiError(response.status, errorBody);
    }
  }

  _backoff(attempt) {
    const base = 400 * 2 ** (attempt - 1);
    return base + Math.random() * base * 0.5;
  }

  _requireSession() {
    if (!this.isSignedIn) throw new Error("Sign in first.");
  }

  async _startSession(session, kek) {
    this.masterKey = await unwrapMasterKey(session.key_bundle.wrapped_master_key, kek);
    this.token = session.token;
    this.username = session.username;
  }

  // -------------------------------------------------------------------------
  // Accounts

  /**
   * Creates an account and signs in. Returns the recovery key, which must be
   * shown to the user once and never stored by the app.
   */
  async register(username, passphrase) {
    const name = normalizeUsername(username);
    const salt = randomBytes(SALT_BYTES);
    const { authKey, kek } = await deriveFromPassphrase(passphrase, salt, this.kdfIterations);
    const recovery = generateRecoveryKey();
    const { recoveryAuth, recoveryKek } = await deriveFromRecoveryKey(recovery.bytes);

    const masterBytes = generateMasterKeyBytes();
    let wrapped;
    let recoveryWrapped;
    try {
      wrapped = await wrapMasterKey(masterBytes, kek);
      recoveryWrapped = await wrapMasterKey(masterBytes, recoveryKek);
    } finally {
      masterBytes.fill(0);
      recovery.bytes.fill(0);
    }

    const session = await this._send("POST", "/api/auth/register", {
      retry: false,
      json: {
        username: name,
        auth_key: b64encode(authKey),
        kdf: KDF_PBKDF2_SHA256,
        iterations: this.kdfIterations,
        salt: b64encode(salt),
        wrapped_master_key: wrapped,
        recovery_wrapped_master_key: recoveryWrapped,
        recovery_auth: b64encode(recoveryAuth),
      },
    });
    await this._startSession(session, kek);
    return { recoveryKey: recovery.display };
  }

  async login(username, passphrase) {
    const name = normalizeUsername(username);
    const params = await this._send("POST", "/api/auth/prelogin", { json: { username: name } });
    if (params.kdf !== KDF_PBKDF2_SHA256) throw new Error(`Unsupported key derivation: ${params.kdf}`);

    const { authKey, kek } = await deriveFromPassphrase(passphrase, b64decode(params.salt), params.iterations);
    const session = await this._send("POST", "/api/auth/login", {
      retry: false,
      json: { username: name, auth_key: b64encode(authKey) },
    });
    await this._startSession(session, kek);
  }

  /**
   * Sets a new passphrase using the recovery key. Signs out every other device.
   * The recovery key itself stays the same.
   */
  async recover(username, recoveryKey, newPassphrase) {
    const name = normalizeUsername(username);
    const recoveryBytes = parseRecoveryKey(recoveryKey);
    const { recoveryAuth, recoveryKek } = await deriveFromRecoveryKey(recoveryBytes);
    recoveryBytes.fill(0);
    const recoveryAuthB64 = b64encode(recoveryAuth);

    // Step 1: prove we hold the recovery key and fetch the master key wrapped under it.
    const bundle = await this._send("POST", "/api/auth/recovery-bundle", {
      retry: false,
      json: { username: name, recovery_auth: recoveryAuthB64 },
    });
    const masterBytes = await unwrapMasterKeyBytes(bundle.recovery_wrapped_master_key, recoveryKek);

    // Step 2: wrap the same master key under the new passphrase and swap it in.
    const salt = randomBytes(SALT_BYTES);
    let authKey;
    let kek;
    let wrapped;
    try {
      ({ authKey, kek } = await deriveFromPassphrase(newPassphrase, salt, this.kdfIterations));
      wrapped = await wrapMasterKey(masterBytes, kek);
    } finally {
      masterBytes.fill(0);
    }

    const session = await this._send("POST", "/api/auth/recover", {
      retry: false,
      json: {
        username: name,
        recovery_auth: recoveryAuthB64,
        auth_key: b64encode(authKey),
        kdf: KDF_PBKDF2_SHA256,
        iterations: this.kdfIterations,
        salt: b64encode(salt),
        wrapped_master_key: wrapped,
      },
    });
    await this._startSession(session, kek);
  }

  async logout() {
    try {
      if (this.token) await this._send("POST", "/api/auth/logout", { expect: "none", retry: false });
    } finally {
      this.token = null;
      this.username = null;
      this.masterKey = null;
    }
  }

  // -------------------------------------------------------------------------
  // Files

  async _fileKeys(file) {
    const raw = await unwrapFileKey(file.wrapped_file_key, this.masterKey, file.id);
    try {
      return await deriveFileKeys(raw);
    } finally {
      raw.fill(0);
    }
  }

  /** Lists files with names decrypted. A name that fails to decrypt comes back as null. */
  async listFiles() {
    this._requireSession();
    const files = await this._send("GET", "/api/files/");
    return Promise.all(
      files.map(async (file) => {
        let name = null;
        try {
          const { nameKey } = await this._fileKeys(file);
          name = await decryptName(file.encrypted_name, nameKey, file.id);
        } catch (error) {
          if (!(error instanceof IntegrityError)) throw error;
        }
        return {
          id: file.id,
          name,
          size: file.size,
          status: file.status,
          segmentCount: file.segment_count,
          createdAt: file.created_at,
          completedAt: file.completed_at,
          // Confirmed copies on storage nodes (null when not on a storage network).
          copies: file.copies ?? null,
          targetCopies: file.target_copies ?? null,
        };
      }),
    );
  }

  /**
   * Encrypts and uploads a Blob or File. Segments are read, encrypted and sent a
   * few at a time, so memory use stays at roughly `concurrency` segments no
   * matter how big the file is.
   *
   * @param {Blob} blob
   * @param {object} [options]
   * @param {string} [options.name]  Defaults to blob.name.
   * @param {number} [options.concurrency]
   * @param {(progress: {sentBytes: number, totalBytes: number}) => void} [options.onProgress]
   * @param {AbortSignal} [options.signal]  Aborting leaves a resumable upload behind.
   * @param {(fileId: string) => void} [options.onCreated]  Called once the file record exists.
   */
  async uploadFile(blob, { name, concurrency = 3, onProgress, signal, onCreated } = {}) {
    this._requireSession();
    const fileId = newFileId();
    const displayName = name ?? blob.name ?? "untitled";

    const rawFileKey = generateFileKeyBytes();
    let wrappedFileKey;
    let keys;
    try {
      wrappedFileKey = await wrapFileKey(rawFileKey, this.masterKey, fileId);
      keys = await deriveFileKeys(rawFileKey);
    } finally {
      rawFileKey.fill(0);
    }

    let manifest;
    try {
      manifest = await this._send("POST", "/api/files/", {
        signal,
        json: {
          id: fileId,
          encrypted_name: await encryptName(displayName, keys.nameKey, fileId),
          wrapped_file_key: wrappedFileKey,
          size: blob.size,
          segment_size: this.segmentSize,
        },
      });
    } catch (error) {
      // A retried create whose first attempt actually landed comes back as 409.
      // The ID is a fresh random UUID, so if we can read it, it's ours.
      if (!(error instanceof ApiError && error.status === 409)) throw error;
      manifest = await this._send("GET", `/api/files/${fileId}`, { signal });
    }
    onCreated?.(fileId);

    return this._uploadSegments(manifest, keys.segmentKey, blob, { concurrency, onProgress, signal });
  }

  /** Continues an interrupted upload. Pass the same file contents as before. */
  async resumeUpload(fileId, blob, { concurrency = 3, onProgress, signal } = {}) {
    this._requireSession();
    const manifest = await this._send("GET", `/api/files/${fileId}`, { signal });
    if (manifest.status === "complete") return manifest;
    const expected = segmentCount(blob.size, manifest.segment_size);
    if (expected !== manifest.segment_count) {
      throw new Error("This doesn't look like the same file: the size doesn't match the interrupted upload.");
    }
    const { segmentKey } = await this._fileKeys(manifest);
    return this._uploadSegments(manifest, segmentKey, blob, { concurrency, onProgress, signal });
  }

  async _uploadSegments(manifest, segmentKey, blob, { concurrency, onProgress, signal }) {
    const { id: fileId, segment_size: size, segment_count: count } = manifest;
    const missing = manifest.missing_segments;
    const plaintextLength = (index) => Math.min(size, blob.size - index * size);

    let sentBytes = blob.size - missing.reduce((n, i) => n + plaintextLength(i), 0);
    onProgress?.({ sentBytes, totalBytes: blob.size });

    await runPool(
      missing,
      concurrency,
      async (index) => {
        const start = index * size;
        const plaintext = new Uint8Array(await blob.slice(start, start + size).arrayBuffer());
        const ciphertext = await encryptSegment(segmentKey, fileId, index, index === count - 1, plaintext);
        plaintext.fill(0);
        await this._send("PUT", `/api/files/${fileId}/segments/${index}`, {
          signal,
          body: ciphertext,
          headers: { "Content-Type": "application/octet-stream", "X-Content-SHA256": await sha256Hex(ciphertext) },
        });
        sentBytes += plaintextLength(index);
        onProgress?.({ sentBytes, totalBytes: blob.size });
      },
      signal,
    );

    return this._send("POST", `/api/files/${fileId}/complete`, { signal });
  }

  /**
   * Downloads, verifies and decrypts a file. Any altered, reordered or missing
   * segment raises IntegrityError instead of returning wrong data.
   *
   * @returns {Promise<{id: string, name: string, size: number, blob: Blob}>}
   */
  async downloadFile(fileId, { concurrency = 3, onProgress, signal, type = "application/octet-stream" } = {}) {
    this._requireSession();
    const manifest = await this._send("GET", `/api/files/${fileId}`, { signal });
    if (manifest.status !== "complete") throw new Error("This file hasn't finished uploading.");

    const count = manifest.segment_count;
    const segments = manifest.segments;
    if (segments.length !== count || segments.some((s, i) => s.index !== i)) {
      throw new IntegrityError("The file's segment list is incomplete or out of order.");
    }

    const { segmentKey, nameKey } = await this._fileKeys(manifest);
    const name = await decryptName(manifest.encrypted_name, nameKey, fileId);

    const parts = new Array(count);
    let receivedBytes = 0;
    onProgress?.({ receivedBytes, totalBytes: manifest.size });

    await runPool(
      segments,
      concurrency,
      async (segment) => {
        const response = await this._send("GET", `/api/files/${fileId}/segments/${segment.index}`, {
          signal,
          expect: "response",
        });
        const ciphertext = new Uint8Array(await response.arrayBuffer());
        if ((await sha256Hex(ciphertext)) !== segment.sha256) {
          throw new IntegrityError(`Segment ${segment.index} doesn't match its recorded hash.`);
        }
        parts[segment.index] = await decryptSegment(
          segmentKey,
          fileId,
          segment.index,
          segment.index === count - 1,
          ciphertext,
        );
        receivedBytes += parts[segment.index].length;
        onProgress?.({ receivedBytes, totalBytes: manifest.size });
      },
      signal,
    );

    return { id: fileId, name, size: receivedBytes, blob: new Blob(parts, { type }) };
  }

  async deleteFile(fileId) {
    this._requireSession();
    try {
      await this._send("DELETE", `/api/files/${fileId}`, { expect: "none" });
    } catch (error) {
      // A retried delete whose first attempt landed finds nothing left to delete.
      if (!(error instanceof ApiError && error.status === 404)) throw error;
    }
  }
}

