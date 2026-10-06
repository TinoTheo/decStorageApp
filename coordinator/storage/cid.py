"""
Content identifiers.

M1 identifies each encrypted segment with a CIDv1 using the `raw` codec and a
SHA-256 multihash — the same shape IPFS uses for a single raw block, written in
base32 (`bafkrei...`). The ID is derived only from the bytes, so anyone holding
a segment can check it matches its ID.

With the IPFS store (Many Homes), Kubo splits anything larger than 1 MiB into
chunks and returns a dag-pb root CID (`bafybei...`) instead. The coordinator
treats the content ID as opaque, stores whatever the active store returns, and
keeps the plain SHA-256 separately for its own integrity checks.
"""

import base64
import binascii
import hashlib
import re

_CID_VERSION_1 = 0x01
_CODEC_RAW = 0x55
_CODEC_DAG_PB = 0x70
_MULTIHASH_SHA2_256 = 0x12
_SHA2_256_LENGTH = 0x20


def cid_v1_raw(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    cid_bytes = bytes([_CID_VERSION_1, _CODEC_RAW, _MULTIHASH_SHA2_256, _SHA2_256_LENGTH]) + digest
    return "b" + base64.b32encode(cid_bytes).decode("ascii").lower().rstrip("=")


_BASE32_LOWER = re.compile(r"^b[a-z2-7]{58}$")


def _decode(value) -> bytes | None:
    if not isinstance(value, str) or not _BASE32_LOWER.match(value):
        return None
    body = value[1:].upper()
    body += "=" * (-len(body) % 8)
    try:
        raw = base64.b32decode(body)
    except (ValueError, binascii.Error):
        return None
    return raw if len(raw) == 36 else None


def is_cid_v1_raw(value: str) -> bool:
    """A CIDv1 for a single raw block, as the local store produces."""
    raw = _decode(value)
    return raw is not None and raw[:4] == bytes([_CID_VERSION_1, _CODEC_RAW, _MULTIHASH_SHA2_256, _SHA2_256_LENGTH])


def is_cid_v1(value: str) -> bool:
    """
    A base32 CIDv1 with a SHA-256 hash, either a raw block or a dag-pb root:
    the two shapes Kubo produces with our fixed `add` settings.
    """
    raw = _decode(value)
    return (
        raw is not None
        and raw[0] == _CID_VERSION_1
        and raw[1] in (_CODEC_RAW, _CODEC_DAG_PB)
        and raw[2:4] == bytes([_MULTIHASH_SHA2_256, _SHA2_256_LENGTH])
    )
