# Running a dstore storage node

A storage node lends disk space to the dstore network. It holds encrypted
pieces of other people's files. **Nobody running a node can read what's on it:**
files are encrypted on the owner's device before they're uploaded, and each
piece is only a fragment of a file.

## What you need

- A computer that stays on most of the time (a desktop, a small server, or a
  mini PC). Windows, macOS or Linux.
- Free disk space: at least 50 GB to be useful. You choose the limit.
- An internet connection. Home broadband or mobile data both work, and you
  don't need to change anything on your router.
- **Docker**: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  on Windows and macOS, or Docker Engine on Linux. On Windows, accept the WSL 2
  option when Docker Desktop asks.
- An **invite** from the network admin: five lines of settings.

## Set it up

1. Put this `node` folder somewhere permanent, e.g. `C:\dstore-node` or `~/dstore-node`.
2. Copy `.env.example` to `.env` (in the same folder).
3. Open `.env` in a text editor (Notepad is fine). Replace the first five lines
   with the five lines from your invite, and set `STORAGE_MAX` to how much space
   you're giving (e.g. `200GB`). Save.
4. Open a terminal in the folder (on Windows: right-click the folder → "Open in
   Terminal") and run:

   ```
   docker compose up -d --build
   ```

5. Check it joined:

   ```
   docker compose logs agent
   ```

   You should see `Registered as node 'harare-1' for operator '...'`. After
   that, `Pinned ...` lines appear as the network gives you pieces to hold.

That's it. It starts again by itself after a reboot, as long as Docker Desktop
is set to start when you log in (Settings → General).

## Day to day

| To... | Run |
|---|---|
| See what it's doing | `docker compose logs -f agent` |
| Stop it for a while | `docker compose stop` |
| Start it again | `docker compose start` |
| Update after the admin sends a new version | `docker compose up -d --build` |
| Change the space limit | edit `STORAGE_MAX` in `.env`, then `docker compose up -d` |

Short outages are fine: the network keeps three copies of everything, each with
a different operator. If you'll be offline for more than a day or two, tell the
network admin.

## Don't

- **Don't delete the Docker volumes** (`docker compose down -v`) unless you're
  leaving the network. They hold the pieces you're keeping and your node's
  identity. If they're wiped, the network treats your copies as lost and you'll
  need a new invite.
- **Don't share `.env`.** It contains the network key and your enrollment code.
- **Don't publish port 5001** or change the `ports:` section beyond `P2P_PORT`.

## Problems

| You see | It means |
|---|---|
| `Waiting for IPFS` for more than a minute | IPFS didn't start: `docker compose logs kubo` |
| `IPFS_SWARM_KEY must be 64 hex characters` | The key in `.env` was cut short when pasting |
| `Registration refused: ... invalid, expired or already used` | Ask the admin for a new invite |
| `The coordinator refused this node` | The node was disabled; contact the admin |
| `Couldn't pin ...` now and then | Usually a slow connection; it's retried automatically |

## Optional: faster transfers

Forwarding port 4001 (TCP) on your router to this computer lets other nodes
connect to you directly instead of through the gateway's relay. It's not
required.
