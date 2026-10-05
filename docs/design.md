# dstore design notes

These notes cover what M1 builds and the decisions behind it. They're written
for whoever picks up M2, and for anyone reviewing the security model.

## What M1 delivers

- A Django coordinator that handles accounts, file records and encrypted segments.
- A browser library (`web/src`) that does all encryption on the device.
- A demo page that uses the same library.
- Segments are staged on the coordinator's own disk. M2 moves them onto storage
  nodes without changing the API or the browser library.

The M1 promise: **the server never holds anything it can decrypt.** The
end-to-end test proves this on every run. It scans the stored bytes for a
plaintext marker and for the filename.

## Threat model

| Party | Can see | Cannot see |
|---|---|---|
| Coordinator and its database | Usernames, file count, file sizes (rounded to segments), upload times, wrapped keys | Passphrases, master keys, file keys, file names, file contents |
| Storage nodes (from M2) | Encrypted segments and their CIDs | Which user or file a segment belongs to, or anything inside it |
| Network observer | TLS-protected traffic only | Everything else |

Out of scope for M1:

- A malicious coordinator serving a modified copy of `client.js`. That's the
  standard limit of any web-delivered end-to-end encryption. The fix is a
  native app or a signed browser extension, which is a product decision for later.
- Hiding file sizes. Padding can be added later if the client needs it.

## Key hierarchy

```
passphrase ──PBKDF2-SHA256 (600k, 16-byte salt)──► root
root ──HKDF "auth"──► auth key        → sent to the server, hashed again there
root ──HKDF "kek"───► KEK             → never leaves the device

recovery key (32 random bytes)
  ──HKDF "recovery-auth"──► recovery auth   → server stores a hash of it
  ──HKDF "recovery-kek"───► recovery KEK    → never leaves the device

master key (32 random bytes)
  wrapped by KEK           → KeyBundle.wrapped_master_key
  wrapped by recovery KEK  → KeyBundle.recovery_wrapped_master_key

file key (32 random bytes, one per file)
  wrapped by master key    → File.wrapped_file_key
  ──HKDF "segment-key"──► segment key  (AES-256-GCM over segments)
  ──HKDF "name-key"─────► name key     (AES-256-GCM over the filename)
```

Why it's built this way:

- **One passphrase, two jobs.** The user types one passphrase. HKDF splits it
  into an auth key and a KEK that are mathematically unrelated, so a server
  holding the auth key learns nothing about the KEK.
- **Passphrase changes are cheap.** Only the master key is re-wrapped; no file
  is re-encrypted.
- **Deleting a file is crypto-shredding.** Once its wrapped file key is gone,
  any leftover copies of its segments are unreadable.
- **Master and file keys are non-extractable** WebCrypto keys once unwrapped.
  Raw key bytes are wiped with `fill(0)` as soon as they're imported.

### Sign-in flow

1. `POST /api/auth/prelogin {username}` returns the KDF salt and iterations.
   Unknown usernames get stable fake values derived with an HMAC, so the
   endpoint can't be used to check which accounts exist.
2. The browser derives the auth key and KEK.
3. `POST /api/auth/login {username, auth_key}` returns a session token and
   the key bundle.
4. The browser unwraps the master key with the KEK.

### Recovery flow

1. `POST /api/auth/recovery-bundle {username, recovery_auth}` returns the
   recovery-wrapped master key, but only after the server checks the recovery
   auth against its stored hash.
2. The browser unwraps the master key, then re-wraps it under a new passphrase.
3. `POST /api/auth/recover` stores the new wrap and the new auth key, and signs
   out every existing session.

If a user loses both the passphrase and the recovery key, their data is
unrecoverable by design. **The client must sign off on this in writing.**

## Segment format

- Files are split into 4 MiB plaintext segments. The last one may be shorter,
  and an empty file is a single empty segment.
- Each segment is encrypted with AES-256-GCM and grows by exactly 16 bytes (the tag).
- Nonce: 4 zero bytes followed by the 64-bit big-endian segment index. This is
  safe because every file has its own segment key, so a (key, nonce) pair never repeats.
- Every segment is bound to its context through the AAD:
  `"dstore/v1/segment:" + fileId || u32(index) || u8(isLast)`.

### What the AAD binds

| Attack | What stops it |
|---|---|
| Swap two segments | The index is in the AAD |
| Move a segment into another file | The file ID is in the AAD |
| Cut the file short and claim it's complete | The last segment is encrypted with `isLast = 1`; any earlier segment presented as the last fails |
| Swap wrapped keys or names between files | The file ID is in their AAD too |

All of these raise `IntegrityError` in the browser. Wrong bytes are never
silently returned.

### Why the browser generates the file ID

The file ID has to go into every ciphertext's AAD before anything is uploaded,
so the browser generates a UUIDv4 and sends it with the create request. The
server rejects duplicate IDs.

## Upload and download

- **Upload:** create the file record, `PUT` segments (three in flight by
  default), then `POST /complete`. Each `PUT` carries `X-Content-SHA256`, and
  the server refuses bytes that don't match it. Retrying the same bytes is a
  no-op. Sending different bytes for an index that's already stored returns 409.
- **Resume:** the manifest lists `missing_segments`. The browser unwraps the
  file key and sends only those.
- **Memory:** only `concurrency` segments (about 12 MB) are held at once
  during upload, which matters on low-end phones. Downloads assemble the file
  in memory in M1, so `MAX_FILE_SIZE` defaults to 1 GB. Streaming downloads
  through a service worker can lift this later.

## Content IDs

`storage/cid.py` computes a CIDv1 (raw codec, SHA-256) for each staged
segment. The test suite checks it against the CID IPFS itself produces for
`hello world`. Kubo splits blocks larger than its chunk size, so from M2 the
coordinator stores whatever CID Kubo returns. It treats the content ID as
opaque and keeps its own SHA-256 for integrity checks.

## Server-side choices

- **Hashed session tokens:** only the SHA-256 of each token is stored, and
  tokens expire 30 days after last use. There's one row per device, so signing
  out on one device doesn't sign out the others.
- **Rate limits:** sign-in, sign-up and recovery are throttled twice. There's a
  generous per-IP limit (120/min), because mobile carriers put many users behind
  one address. There's also a tight per-username limit (10/min), which is what
  actually slows passphrase guessing, wherever the guesses come from. Behind
  Caddy, set `NUM_PROXIES=1` (the compose file already does). M1 uses Django's
  per-process cache, so each limit is multiplied by the number of gunicorn
  workers. In M2, Redis makes them single shared limits.
- **Content Security Policy:** the page that handles keys runs no inline
  scripts (`script-src 'self'`).
- **Admin:** shows IDs, sizes and statuses only. Wrapped keys and encrypted
  names are hidden.

## What M2 changes

- `storage/backends.py` gets a `KuboSegmentStore`. The gateway adds a segment
  to IPFS, the coordinator picks three nodes from different operators, their
  agents pin by CID, and the gateway unpins its copy.
- New models: `Operator`, `Node`, `Replica (segment, node, status, last_verified)`.
- Celery and Redis come in for placement and cleanup of abandoned uploads.
  Redis also backs the rate limiter.
- File deletion schedules an unpin on every node holding a replica.

## Known limitations in M1

- Abandoned uploads stay until the user deletes them. A cleanup job comes with Celery in M2.
- There's no passphrase change while signed in, only via recovery. That's planned for M4.
- The file list isn't paginated.
- Only PBKDF2 is supported. Argon2id (via WebAssembly) can be added as a new `kdf` value without a migration.
