import base64
import binascii

from rest_framework import serializers

# Layout of everything the browser wraps with AES-256-GCM:
# 1 version byte + 12-byte nonce + 32-byte key + 16-byte tag.
WRAPPED_KEY_BYTES = 1 + 12 + 32 + 16


def decode_b64(value: str) -> bytes:
    """Strict standard base64 decode. Raises ValueError on anything malformed."""
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("not valid base64") from exc


class Base64BytesField(serializers.CharField):
    """
    A base64 string checked for the decoded length it must have.
    The canonical string is stored as-is; the server never needs the raw bytes.
    """

    def __init__(self, *, exact_length=None, min_length_bytes=None, max_length_bytes=None, **kwargs):
        self.exact_length = exact_length
        self.min_length_bytes = min_length_bytes
        self.max_length_bytes = max_length_bytes
        kwargs.setdefault("trim_whitespace", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            raw = decode_b64(value)
        except ValueError:
            self.fail("invalid_b64")
        if self.exact_length is not None and len(raw) != self.exact_length:
            self.fail("wrong_length", expected=self.exact_length, actual=len(raw))
        if self.min_length_bytes is not None and len(raw) < self.min_length_bytes:
            self.fail("too_short", minimum=self.min_length_bytes)
        if self.max_length_bytes is not None and len(raw) > self.max_length_bytes:
            self.fail("too_long", maximum=self.max_length_bytes)
        return base64.b64encode(raw).decode("ascii")

    default_error_messages = {
        "invalid_b64": "Must be standard base64.",
        "wrong_length": "Must decode to {expected} bytes, got {actual}.",
        "too_short": "Must decode to at least {minimum} bytes.",
        "too_long": "Must decode to at most {maximum} bytes.",
    }


class WrappedKeyField(Base64BytesField):
    def __init__(self, **kwargs):
        super().__init__(exact_length=WRAPPED_KEY_BYTES, **kwargs)


class Key256Field(Base64BytesField):
    """A 32-byte secret derived in the browser (auth keys, recovery verifiers)."""

    def __init__(self, **kwargs):
        super().__init__(exact_length=32, **kwargs)
