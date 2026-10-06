#!/bin/sh
# Configures IPFS (Kubo) for the dstore private network. Runs on every start,
# so changes to .env take effect after a restart.
#
#   DSTORE_ROLE=node     a storage node (default)
#   DSTORE_ROLE=gateway  the coordinator's gateway: the hub nodes connect to
set -eu

ROLE="${DSTORE_ROLE:-node}"
: "${DSTORE_SWARM_KEY:?IPFS_SWARM_KEY is missing from .env (it comes with your invite)}"
REPO="${IPFS_PATH:-/data/ipfs}"

warn() { echo "dstore: warning: $*" >&2; }
try() { "$@" || warn "couldn't run: $*"; }

# 1. Join the private network. Kubo refuses to start without this file because
#    LIBP2P_FORCE_PNET=1, so a node can never end up on the public IPFS network.
KEY=$(printf '%s' "$DSTORE_SWARM_KEY" | tr -d '[:space:]' | tr 'A-F' 'a-f')
case "$KEY" in
  *[!0-9a-f]*|"") echo "dstore: IPFS_SWARM_KEY must be 64 hex characters" >&2; exit 1 ;;
esac
if [ "${#KEY}" -ne 64 ]; then echo "dstore: IPFS_SWARM_KEY must be 64 hex characters" >&2; exit 1; fi
rm -f "$REPO/swarm.key"
printf '/key/swarm/psk/1.0.0/\n/base16/\n%s\n' "$KEY" > "$REPO/swarm.key"
chmod 0400 "$REPO/swarm.key"

# 2. Talk only to our own network: no public bootstrap peers, routers or resolvers.
ipfs bootstrap rm --all >/dev/null
if [ "$ROLE" = "node" ]; then
  : "${DSTORE_BOOTSTRAP:?GATEWAY_BOOTSTRAP is missing from .env (it comes with your invite)}"
  ipfs bootstrap add "$DSTORE_BOOTSTRAP" >/dev/null
fi
try ipfs config --json AutoConf.Enabled false
try ipfs config --json Routing.DelegatedRouters '[]'
try ipfs config --json Ipns.DelegatedPublishers '[]'
try ipfs config --json DNS.Resolvers '{}'
ipfs config Routing.Type dht
# In a network this small, asking every connected peer for a block is cheapest.
try ipfs config --json Internal.Bitswap.BroadcastControl.Enable false

# 3. Private networks run over plain TCP only: no QUIC, WebTransport, WebRTC or
#    WebSocket (sharing the TCP port with WebSocket isn't supported with a swarm
#    key), and no AutoTLS certificates (a public-network feature).
ipfs config --json Addresses.Swarm '["/ip4/0.0.0.0/tcp/4001", "/ip6/::/tcp/4001"]'
try ipfs config --json Swarm.Transports.Network.QUIC false
try ipfs config --json Swarm.Transports.Network.WebTransport false
try ipfs config --json Swarm.Transports.Network.WebRTCDirect false
try ipfs config --json Swarm.Transports.Network.Websocket false
try ipfs config --json AutoTLS.Enabled false

# 4. The RPC API is an admin interface: it listens on the container network only
#    (never published), and there is no public HTTP gateway.
ipfs config Addresses.API /ip4/0.0.0.0/tcp/5001
ipfs config --json Addresses.Gateway '[]'

# 5. Space and relaying. Nodes behind home routers or mobile networks reach each
#    other through the gateway's relay.
ipfs config Datastore.StorageMax "${DSTORE_STORAGE_MAX:-50GB}"
ipfs config --json Datastore.StorageGCWatermark 90
if [ "$ROLE" = "gateway" ]; then
  ipfs config --json Swarm.RelayService.Enabled true
  try ipfs config --json Swarm.RelayClient.Enabled false
else
  try ipfs config --json Swarm.RelayService.Enabled false
  try ipfs config --json Swarm.RelayClient.Enabled true
  try ipfs config --json Swarm.RelayClient.StaticRelays "[\"$DSTORE_BOOTSTRAP\"]"
fi

echo "dstore: IPFS configured as $ROLE (storage limit ${DSTORE_STORAGE_MAX:-50GB})"
