# Changelog

Each release gets a plain name that says what it does.

| Release | What it means | Status |
|---|---|---|
| **Locked Box** | Files are locked on your device before they leave it | Released 5 Oct 2026 |
| Many Homes | Locked files spread across independent storage nodes | Next |
| Self Repair | The network notices lost copies and replaces them | Planned |
| Front Door | Polished app for users and a dashboard for node operators | Planned |
| Test Drive | Real nodes, real users, a three-week trial run | Planned |

## Locked Box (5 Oct 2026)

The first release. Files are encrypted in the browser and the server only ever
stores ciphertext. For now the encrypted pieces sit on the coordinator; Many
Homes moves them onto storage nodes.

**Added**
- Django coordinator API for accounts, file records and encrypted segments.
- Sign-up, sign-in and recovery where the server never sees the passphrase or any key.
- Recovery key shown once at sign-up. It resets the passphrase and signs out every device.
- Browser encryption library: AES-256-GCM over 4 MB segments. Each segment is
  bound to its file, its position and the end of the file, so tampering,
  reordering or truncation is detected.
- Encrypted file names, with a per-file key wrapped by the user's master key.
- Parallel, resumable uploads with SHA-256 checks on every segment.
- Verified downloads that refuse altered data instead of returning it.
- Demo web page, working on mobile and desktop, under a strict Content-Security-Policy.
- Hashed, expiring session tokens, one per device.
- Rate limits per IP and per username, so mobile users sharing an IP aren't blocked.
- Docker Compose stack with Postgres and Caddy for automatic HTTPS.

**Tested**
- 48 server tests, passing on SQLite and Postgres 16.
- 25 encryption tests.
- 13 end-to-end checks against a live server, including proof that no plaintext or filename reaches it.
- Manual run in headless Chrome at phone and desktop sizes.

**Not yet**
- Docker images haven't been built yet. Do that on the staging VM first.
- Abandoned uploads aren't cleaned up automatically (Many Homes).
- No passphrase change while signed in (Front Door).
- Downloads load the whole file into memory, so files are capped at 1 GB.
