# dstore design notes

These notes cover what's been built so far and the decisions behind it. They're
written for whoever picks up the next release, and for anyone reviewing the
security model. Day-to-day running of the network is in `operations.md`.

## Releases so far

**Locked Box** (M1): a Django coordinator for accounts, file records and
encrypted segments; a browser library (`web/src`) that does all encryption on
the device; a demo page using the same library. Promise: **the server never
holds anything it can decrypt.**

**Many Homes** (M2): encrypted segments leave the coordinator and live on
independent storage nodes, three copies each, every copy with a different
operator. Nodes run IPFS (Kubo) plus a small agent. Promise: **no single
machine and no single operator holds the only copy of anything, and none of
them can read what they hold.**

Both promises are checked on every test run: the end-to-end tests scan what the
server and the nodes actually stored for a plaintext marker and the filename.

## Threat model

| Party | Can see | Cannot see |
|---|---|---|
| Coordinator and its database | Usernames, file count, file sizes (rounded to segments), upload times, wrapped keys | Passphrases, master keys, file keys, file names, file contents |
| Storage nodes and their operators | Encrypted segments, their CIDs and sizes | Which user or file a segment belongs to, or anything inside it |
| Other nodes on the private network | That a CID exists, which peers hold it | Anything inside it |
| Network observer | TLS-protected traffic only | Everything else |

Out of scope for now:

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

`storage/cid.py` computes a CIDv1 (raw codec, SHA-256) for segments staged on
local disk. The test suite checks it against the CID IPFS itself produces for
`hello world`. With the IPFS store, Kubo adds every segment with fixed settings
(CIDv1, raw leaves, 1 MiB chunks, SHA-256), so the same bytes give the same CID
on every node. Segments over 1 MiB come back as a dag-pb root (`bafybei...`).
The coordinator treats the content ID as opaque and keeps its own SHA-256 for
integrity checks.

## Server-side choices

- **Hashed session tokens:** only the SHA-256 of each token is stored, and
  tokens expire 30 days after last use. There's one row per device, so signing
  out on one device doesn't sign out the others.
- **Rate limits:** sign-in, sign-up and recovery are throttled twice. There's a
  generous per-IP limit (120/min), because mobile carriers put many users behind
  one address. There's also a tight per-username limit (10/min), which is what
  actually slows passphrase guessing, wherever the guesses come from. Behind
  Caddy, set `NUM_PROXIES=1` (the compose file already does). The counts live
  in a database-backed cache, so every gunicorn worker shares them.
- **Content Security Policy:** the page that handles keys runs no inline
  scripts (`script-src 'self'`).
- **Admin:** shows IDs, sizes and statuses only. Wrapped keys and encrypted
  names are hidden.

## Many Homes: the storage network

### Pieces

```
browser ──HTTPS──► coordinator ──RPC──► gateway IPFS ◄──private IPFS network──► storage nodes
                       ▲                                                        (IPFS + agent)
                       └──────────── agents check in over HTTPS ◄───────────────────┘
```

- **Gateway IPFS node** (`ipfs` service, next to the coordinator). New uploads
  are added here first. It's the hub nodes connect to and their relay when
  they're behind a home router. Its RPC API is never published.
- **Storage node** = Kubo + the agent (`node/`). Operators install it with
  Docker. Kubo is configured by `node/kubo/001-dstore.sh` for the private
  network: swarm key required (`LIBP2P_FORCE_PNET=1`), no public bootstrap
  peers, routers or resolvers, TCP only, no public HTTP gateway.
- **Agent** (`node/agent/dstore_agent.py`, standard library only). It always
  calls the coordinator, never the other way round, so nodes need no port
  forwarding: register once with an invite code, then heartbeat every few
  seconds and do the pin and unpin tasks the coordinator hands back.
- **Worker** (`manage.py run_worker`). One loop does all background work:
  placement, timeouts, gateway release, deletions, abandoned uploads, gateway
  garbage collection. It replaces the Celery and Redis plan: there's no fan-out
  to justify them, and one fewer service to keep running on a single VM.

### Data model (`network` app)

| Model | Purpose |
|---|---|
| `Operator` | A person or organisation running nodes. The unit copies are spread across. |
| `NodeInvite` | Single-use enrollment code (stored hashed), expires after a week by default. |
| `Node` | One machine: IPFS peer ID, hashed token, capacity and usage, last check-in. |
| `Blob` | One encrypted segment on the network, by CID. Segments refer to it by CID. |
| `Replica` | One node's copy of one blob: `assigned` → `stored`, or `releasing` → deleted; `lost` if the node can't be trusted any more. |

### A file's life on the network

1. **Upload.** The coordinator adds each segment to the gateway IPFS node
   (pinned) and records a `Blob`.
2. **Placement** (worker). Each blob gets `REPLICA_COUNT` (3) copies:
   - only on online, active nodes whose operator is active,
   - never two live copies with the same operator (`REQUIRE_DISTINCT_OPERATORS`),
   - only where the node has room, counting copies already on their way,
   - preferring the nodes with the most free space, with a little randomness.
   A blob that can't get all its copies yet (too few operators online) keeps
   what it has and is retried on every pass.
3. **Pinning** (agent). On its next heartbeat the node gets `pin` tasks and
   runs `ipfs pin add`, which fetches the blocks over the private network and
   checks every one against its CID. It reports success or failure. Failures
   are retried `PIN_MAX_ATTEMPTS` times, then the copy is moved elsewhere. A
   copy not confirmed within `ASSIGNMENT_TIMEOUT_SECONDS` is moved too.
4. **Gateway release** (worker). Once three nodes have confirmed, the gateway
   unpins its copy. Garbage collection reclaims the space later.
5. **Download.** The coordinator asks the gateway IPFS node for the segment,
   which fetches it from whichever node answers. The coordinator checks the
   SHA-256 before sending anything to the browser: damaged or unreachable
   copies give a 503 (the browser retries), never wrong bytes.
6. **Delete.** Every copy becomes `releasing`, and the gateway drops its copy
   if it still has one. Nodes unpin on their next heartbeat; a node that's
   offline does it when it comes back. The `Blob` is removed once no node
   holds it.
7. **Abandoned uploads.** Files still `uploading` after
   `ABANDONED_UPLOAD_AFTER_HOURS` (48) are deleted, copies and all.

### Node API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/nodes/register` | invite code | Enroll; returns the node token (shown once) |
| POST | `/api/nodes/heartbeat` | `Node <token>` | Report status, receive tasks |
| POST | `/api/nodes/tasks/{id}` | `Node <token>` | Report a pin or unpin result |

Node tokens and user tokens are separate schemes: neither works on the other's
endpoints. Results for work the coordinator no longer wants (say, a pin that
finished after the file was deleted) are ignored, and the node gets the
follow-up task next time. Pin and unpin are both idempotent, so a lost result
costs nothing.

If a node's IPFS identity changes (its storage was reset), its copies are
marked `lost`, placement replaces them elsewhere, and the node is disabled
until it's re-enrolled with a new invite.

### What Many Homes doesn't do yet

- **Nobody checks that nodes still hold what they confirmed.** A node could
  delete data and keep heartbeating. Spot-checks come in Self Repair.
- **A node that stays offline keeps its copies counted.** Self Repair moves
  copies off nodes that have been gone too long.
- **Re-replication after a loss** works when the remaining copies are
  reachable, but nothing measures how long it takes or alerts on it.
- **Not yet run across real networks.** The network test passes on real Kubo
  0.43.1 in private-network mode (config, bitswap transfers, gateway release,
  downloads from nodes, node loss, deletes), but on one machine. NAT traversal
  through the gateway's relay, and the Docker images, need a run on real
  machines; see `operations.md`, "First run on real machines".

## Known limitations

- There's no passphrase change while signed in, only via recovery. Planned for Front Door.
- The file list isn't paginated.
- Downloads assemble the whole file in memory, so files are capped at 1 GB.
- Only PBKDF2 is supported. Argon2id (via WebAssembly) can be added as a new `kdf` value without a migration.
