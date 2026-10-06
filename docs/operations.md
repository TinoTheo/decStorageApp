# Running the storage network

For whoever runs the coordinator VM. Node operators have their own guide in
`node/README.md`.

All commands run on the coordinator VM from the repo folder. `dc` below is
short for `docker compose`.

## First-time setup

1. **Create the network key** and put it in `.env`:

   ```bash
   dc run --rm coordinator python manage.py create_swarm_key
   # copy the IPFS_SWARM_KEY=... line into .env
   ```

   Anyone with this key can join the IPFS network (but can't read files:
   they're encrypted). Keep it out of chat groups and screenshots.

2. **Open the firewall** for:
   - `443/tcp` and `80/tcp` (HTTPS for users and node agents)
   - `4001/tcp` (IPFS: nodes connect here)

   Never open `5001`: that's the IPFS admin API.

3. **Start everything:**

   ```bash
   dc up -d --build
   dc logs -f worker      # "Worker started"
   dc logs ipfs | grep dstore   # "IPFS configured as gateway"
   ```

## Adding a node

Each node needs an invite. The operator is created the first time you name them.

```bash
dc exec coordinator python manage.py invite_node --operator "Thandi Moyo" --contact "+263 77 000 0000" --node-name harare-1
```

It prints five lines (`COORDINATOR_URL`, `ENROLLMENT_CODE`, `NODE_NAME`,
`IPFS_SWARM_KEY`, `GATEWAY_BOOTSTRAP`). Send them to the operator privately,
together with the `node/` folder. The code works once and expires after 7 days
(`--ttl-hours` to change that).

**Copies always go to different operators.** Until at least three different
operators have a node online, files stay "spreading" and the gateway keeps its
copies. Two nodes run by the same person count as one operator.

## Checking on the network

```bash
dc exec coordinator python manage.py network_status
```

```
Nodes (4)
  ama-1            Ama                online    seen 3s ago   1.2 GB / 50.0 GB  310 stored
  ama-2            Ama                online    seen 5s ago   0.9 GB / 50.0 GB  240 stored, 2 assigned
  bongani-1        Bongani            offline   seen 2h ago   2.1 GB / 200.0 GB  550 stored
  chipo-1          Chipo              online    seen 4s ago   2.0 GB / 100.0 GB  550 stored

Segments: 550 live, 548 with 3+ copies, 2 still spreading, 2 still on the gateway, 0 being deleted
```

Add `--json` for monitoring scripts. The Django admin (`/admin`) shows the same
information under "Storage network", plus each segment's copies.

What to watch:

- **"still spreading" stays high:** fewer than three operators online, or
  nodes are full. The status output warns about the first case.
- **"still on the gateway" keeps growing:** the gateway disk fills up (limit:
  `GATEWAY_STORAGE_MAX`). Same causes as above.
- **A node "offline" for days:** contact the operator. Its copies still count
  for now; Self Repair will move them automatically.

## Removing or pausing a node

In the admin, select the node and choose **Disable**. It gets no new copies.
Its existing copies keep counting, so only disable a node you expect back, or
one whose data you're about to move. Moving data off a node for good is part of
Self Repair.

If an operator resets their node's storage, its IPFS identity changes. The
coordinator notices on the next check-in, marks its copies lost (they're
re-placed elsewhere) and disables the node. Send the operator a new invite and
ask them to delete their `agent` volume (`docker compose down -v` in `node/`
removes everything, including the stored data).

## Settings that matter

All in `.env`; restart with `dc up -d` after changing them.

| Setting | Default | What it does |
|---|---|---|
| `REPLICA_COUNT` | 3 | Copies per segment |
| `REQUIRE_DISTINCT_OPERATORS` | 1 | Each copy with a different operator. Keep on in production. |
| `GATEWAY_STORAGE_MAX` | 20GB | Gateway disk for uploads not yet on nodes |
| `ABANDONED_UPLOAD_AFTER_HOURS` | 48 | Unfinished uploads are deleted after this |
| `NODE_OFFLINE_AFTER_SECONDS` | 90 | Silence before a node gets no new copies |
| `ASSIGNMENT_TIMEOUT_SECONDS` | 3600 | A copy not confirmed by then goes elsewhere |
| `PLACEMENT_HEADROOM_BYTES` | 64 MB | Space a node must keep free after taking a segment |

## Backups

Back up the `pgdata` volume daily. It holds accounts, wrapped keys and the map
of which node holds what. **Without it, the copies on the nodes are useless:**
nobody can find or decrypt them. The `ipfs` volume only holds uploads still on
their way to nodes, so it matters less, but back it up if you can.

## Moving from a Locked Box deployment

Segments uploaded under Locked Box sit in the coordinator's `staging` volume.
After upgrading:

```bash
dc exec coordinator python manage.py import_local_segments --staging-dir /data/staging
```

It copies each one into IPFS (safe to run twice) and placement takes it from
there. Archive the staging volume once `network_status` shows no segments
"still on the gateway".

## Testing on real IPFS

The network test runs on real Kubo as well as on the stand-in. On Linux or
macOS (or WSL on Windows), download Kubo 0.43.1 for your platform from
https://github.com/ipfs/kubo/releases, unpack it, and run:

```bash
IPFS_BIN=/path/to/kubo/ipfs python tasks.py network-test
```

It starts real IPFS nodes configured by the same script the Docker images use
(`node/kubo/001-dstore.sh`), in private-network mode, and runs all 9 checks.
This passed on Kubo 0.43.1 on 6 Oct 2026.

**Addresses: why nodes need public ones.** Nodes and the gateway use IPFS's
"server" profile, which refuses to connect to private addresses (10.x, 172.16–31.x,
192.168.x, 127.x). That stops nodes probing operators' home networks, but it
means:

- `GATEWAY_P2P_HOST` must be a public DNS name or IP, never an internal one
  (for example a cloud VM's 10.x address).
- Two nodes on the same office or home network still talk to each other through
  their public address or the gateway's relay, not directly over the LAN.

The local test lifts that rule for its own nodes only, because everything runs
on 127.0.0.1.

## First run on real machines

What the automated tests can't cover: building the Docker images, and nodes on
different networks behind real routers. Do this once on staging before
inviting real operators:

1. Bring up the coordinator stack. `dc logs ipfs` should show "IPFS configured
   as gateway", "Swarm is limited to private network" and no `ERROR` lines.
2. Enroll three nodes from three operators, ideally one behind a home router
   and one on mobile data. Each agent log should say "Registered as node".
3. On each node, `docker compose exec kubo ipfs swarm peers` lists the gateway.
4. Upload a file of 10 MB or more on the demo page. Within a minute it should
   show "3 copies on separate nodes", and `network_status` should show nothing
   on the gateway.
5. Run `dc exec ipfs ipfs repo gc` on the gateway, then download the file. It
   must come back from the nodes.
6. Switch off one node and download again.
7. Delete the file. On every node, `ipfs pin ls --type=recursive` should show
   only the built-in empty-folder pin (`QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn`).

If step 4 stalls, check the agent log for pin errors and that port 4001 is open
on the coordinator VM. If step 5 is slow, look at `ipfs routing findprovs <cid>`
on the gateway: nodes should be listed as providers.
