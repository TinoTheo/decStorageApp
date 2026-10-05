"""
Content identifiers.

M1 identifies each encrypted segment with a CIDv1 using the `raw` codec and a
SHA-256 multihash — the same shape IPFS uses for a single raw block, written in
base32 (`bafkrei...`). The ID is derived only from the bytes, so anyone holding
a segment can check it matches its ID.

Note: once segments are pinned through Kubo in M2, Kubo chunks anything larger
than its block size and returns a dag-pb root CID instead. The coordinator treats
the content ID as opaque and stores whatever the active backend returns, and keeps
the plain SHA-256 separately for its own integrity checks.
"""

import base64
import binascii
import hashlib

_CID_VERSION_1 = 0x01
_CODEC_RAW = 0x55
_MULTIHASH_SHA2_256 = 0x12
_SHA2_256_LENGTH = 0x20


def cid_v1_raw(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    cid_bytes = bytes([_CID_VERSION_1, _CODEC_RAW, _MULTIHASH_SHA2_256, _SHA2_256_LENGTH]) + digest
    return "b" + base64.b32encode(cid_bytes).decode("ascii").lower().rstrip("=")


def is_cid_v1_raw(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("b"):
        return False
    body = value[1:].upper()
    body += "=" * (-len(body) % 8)
    try:
        raw = base64.b32decode(body)
    except (ValueError, binascii.Error):
        return False
    return len(raw) == 36 and raw[:4] == bytes(
        [_CID_VERSION_1, _CODEC_RAW, _MULTIHASH_SHA2_256, _SHA2_256_LENGTH]
    )
