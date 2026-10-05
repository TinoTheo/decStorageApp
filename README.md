# dstore

End-to-end encrypted storage that will spread files across independent
storage nodes. This repository is **Milestone 1**: the coordinator, the
browser encryption library, and a working demo where files are encrypted on
the device and the server only ever stores ciphertext.

```
coordinator/   Django + DRF API: accounts, file records, encrypted segments
web/src/       Browser crypto and API client (plain ES modules, no build step)
scripts/       End-to-end test against a live server
tasks.py       Project commands (install, dev, test) for any OS
deploy/        Caddy config (HTTPS)
docs/          Design notes and security model
node/          Storage node package (arrives in M2)
```

## Run it locally

Needs Python 3.10+ and, for the tests, Node.js 20+. The same commands work on
Windows, macOS and Linux. On Windows, use `py` instead of `python` if
`python` isn't recognised.

```bash
python tasks.py install   # install the Python packages
python tasks.py dev       # start on http://127.0.0.1:8000
```

Open http://127.0.0.1:8000, create an account, save the recovery key, and
upload a file. Browsers only allow WebCrypto on `localhost` or HTTPS, so use
`127.0.0.1` or `localhost`, not your LAN IP.

## Tests

```bash
python tasks.py test      # all three suites
python tasks.py test-py   # 48 server tests: auth, files, segments, storage, isolation
python tasks.py test-js   # 25 encryption tests: tampering, reordering, truncation, recovery
python tasks.py e2e       # 13 end-to-end checks against a throwaway live server
```

On macOS and Linux, `make test` and friends do the same thing.

The server suite passes on both SQLite and Postgres 16.

The end-to-end run registers a user, interrupts an upload and resumes it,
downloads and compares the bytes, then:

- scans the server's stored bytes to prove no plaintext or filename reached it
- flips a bit on disk and confirms the download is refused
- checks that a second account can't see the file
- recovers the account with the recovery key

## Deploy to a staging VM

Use a VM on the **client's** account, with Docker installed and ports 80 and
443 open, and a DNS record pointing at it.

```bash
cp .env.example .env    # set DOMAIN, DJANGO_SECRET_KEY, POSTGRES_PASSWORD, hosts
docker compose up -d --build
docker compose exec coordinator python manage.py createsuperuser   # for /admin
```

Caddy gets an HTTPS certificate for `DOMAIN` automatically. Encrypted segments
live in the `staging` volume. **Back up both the `pgdata` and `staging`
volumes**: the database holds the wrapped keys, and without them the
segments are unreadable.

Generate a secret key with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

## API at a glance

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/prelogin` | KDF salt and iterations for a username |
| POST | `/api/auth/register` | Create an account with a client-built key bundle |
| POST | `/api/auth/login` | Exchange an auth key for a session token |
| POST | `/api/auth/recovery-bundle` | Recovery step 1: fetch the recovery-wrapped master key |
| POST | `/api/auth/recover` | Recovery step 2: set a new passphrase |
| POST | `/api/auth/logout` | End this device's session |
| GET | `/api/auth/me` | Current user and key bundle |
| GET / POST | `/api/files/` | List files / create a file record |
| GET / DELETE | `/api/files/{id}` | Manifest (including missing segments) / delete |
| PUT / GET | `/api/files/{id}/segments/{n}` | Upload (with `X-Content-SHA256`) / download a segment |
| POST | `/api/files/{id}/complete` | Mark the upload finished |

Authenticated requests send `Authorization: Token <token>`.

## Using the client library

```js
import { DStoreClient } from "./web/src/client.js";

const client = new DStoreClient({ baseUrl: "https://storage.example.com" });
const { recoveryKey } = await client.register("thandi", "four unrelated words here");
const manifest = await client.uploadFile(file, { onProgress: ({ sentBytes, totalBytes }) => {} });
const { name, blob } = await client.downloadFile(manifest.id);
```

See `docs/design.md` for the key hierarchy, segment format and threat model.
