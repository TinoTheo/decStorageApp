# IPFS (Kubo) preconfigured for the dstore private network.
# Used for storage nodes and, with DSTORE_ROLE=gateway, for the coordinator's gateway.
FROM ipfs/kubo:v0.43.1

COPY 001-dstore.sh /container-init.d/001-dstore.sh
# Windows checkouts can turn line endings into CRLF, which breaks shell scripts.
RUN sed -i 's/\r$//' /container-init.d/001-dstore.sh && chmod 0755 /container-init.d/001-dstore.sh

# Refuse to start without a swarm key: never join the public IPFS network by accident.
ENV LIBP2P_FORCE_PNET=1 \
    IPFS_PROFILE=server

CMD ["daemon", "--migrate=true", "--enable-gc"]
