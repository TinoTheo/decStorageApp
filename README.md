## dstore-first take 

Encrypted file storage that doesn't ask you to trust any single machine. Filesare encrypted on your device, split into pieces, and each piece is kept by threestorage nodes run by three different operators. No server, and no node, canread any of it.

Current release: Many Homes

```
coordinator/   Django + DRF API: accounts, files, the storage network, background workerweb/src/       Browser crypto and API client (plain ES modules, no build step)node/          What node operators run: IPFS (Kubo) + the dstore agentscripts/       End-to-end and network teststasks.py       Project commands (install, dev, test), the same on any OSdeploy/        Caddy config (HTTPS)docs/          design.md (how it works, security model), operations.md (running the network)
```
## Run it locally

You'll need Python 3.10+, plus Node.js 20+ .

python tasks.py install   # install the Python packagespython tasks.py dev       # start on http://127.0.0.1:8000
Open http://127.0.0.1:8000, create an account, write the recovery key downsomewhere safe, and upload a file. One quirk: browsers only allow WebCrypto onlocalhost or HTTPS, so use 127.0.0.1 or localhost, not your LAN IP.

Local development keeps encrypted segments on disk (SEGMENT_STORE=local), soyou don't need IPFS. If you'd like to watch a whole small network run on onemachine, try python tasks.py network-test.

## Tests

python tasks.py test           # everything below
python tasks.py test-py        # 95 server tests: auth, files, placement, node API, worker, storage
python tasks.py test-js        # 25 encryption tests: tampering, reordering, truncation, recovery
python tasks.py test-agent     # 6 node agent tests
python tasks.py e2e            # 13 end-to-end checks against a throwaway server
python tasks.py network-test   # 9 checks on a whole network running on this machine

