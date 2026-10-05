# Storage node (M2)

This folder will hold the package each storage operator runs:

- **Kubo (IPFS)** on a private network, joined with a shared swarm key, so
  nodes only talk to each other and the gateway.
- **A small Python agent** that registers the node with the coordinator, sends
  health pings, pins and unpins segments by CID, and answers spot-checks.
- **A Docker Compose file** so an operator can install it with one command.

Nodes only ever receive encrypted segments. They can't tell which user or
file a segment belongs to. See `docs/design.md`, "What M2 changes".
