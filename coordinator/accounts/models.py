import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class KeyBundle(models.Model):
    """
    Everything the browser needs to rebuild a user's master key — and nothing
    that lets the server do it.

    The master key is wrapped twice: once with a key derived from the passphrase,
    once with a key derived from the recovery key. Both derivations happen in the
    browser; the server only ever sees the wrapped results.
    """

    KDF_PBKDF2_SHA256 = "pbkdf2-sha256"
    KDF_CHOICES = [(KDF_PBKDF2_SHA256, "PBKDF2-SHA256")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="key_bundle")
    kdf = models.CharField(max_length=32, choices=KDF_CHOICES, default=KDF_PBKDF2_SHA256)
    kdf_iterations = models.PositiveIntegerField()
    kdf_salt = models.CharField(max_length=64)
    wrapped_master_key = models.CharField(max_length=128)
    recovery_wrapped_master_key = models.CharField(max_length=128)
    # A hash of a value derived from the recovery key, so the server can check a
    # recovery attempt without ever holding the recovery key itself.
    recovery_verifier = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Key bundle for {self.user}"

    def public_params(self):
        return {"kdf": self.kdf, "iterations": self.kdf_iterations, "salt": self.kdf_salt}

    def as_payload(self):
        return {
            **self.public_params(),
            "wrapped_master_key": self.wrapped_master_key,
            "recovery_wrapped_master_key": self.recovery_wrapped_master_key,
        }


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuthTokenQuerySet(models.QuerySet):
    def live(self):
        return self.filter(expires_at__gt=timezone.now())


class AuthToken(models.Model):
    """
    One row per signed-in device. Only a SHA-256 of the token is stored, so a
    database leak doesn't hand out working sessions. Expiry slides forward with use.
    """

    TTL = timedelta(days=30)
    TOUCH_INTERVAL = timedelta(minutes=5)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="auth_tokens")
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    objects = AuthTokenQuerySet.as_manager()

    @classmethod
    def issue(cls, user):
        raw = secrets.token_urlsafe(32)
        now = timezone.now()
        token = cls.objects.create(user=user, key_hash=_hash_token(raw), last_used_at=now, expires_at=now + cls.TTL)
        return token, raw

    @classmethod
    def lookup(cls, raw):
        return cls.objects.live().select_related("user").filter(key_hash=_hash_token(raw)).first()

    def touch(self):
        now = timezone.now()
        if now - self.last_used_at >= self.TOUCH_INTERVAL:
            type(self).objects.filter(pk=self.pk).update(last_used_at=now, expires_at=now + self.TTL)
