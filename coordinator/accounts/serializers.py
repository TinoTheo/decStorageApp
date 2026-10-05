from django.conf import settings
from django.contrib.auth.validators import UnicodeUsernameValidator
from rest_framework import serializers

from .fields import Base64BytesField, Key256Field, WrappedKeyField
from .models import KeyBundle

MAX_KDF_ITERATIONS = 10_000_000


def normalize_username(value: str) -> str:
    """The browser applies the same rule before deriving keys."""
    return value.strip().lower()


class UsernameField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("min_length", 3)
        kwargs.setdefault("max_length", 150)
        kwargs.setdefault("validators", [UnicodeUsernameValidator()])
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = normalize_username(data)
        return super().to_internal_value(data)


class KdfParamsMixin(serializers.Serializer):
    kdf = serializers.ChoiceField(choices=[c[0] for c in KeyBundle.KDF_CHOICES])
    iterations = serializers.IntegerField(max_value=MAX_KDF_ITERATIONS)
    salt = Base64BytesField(exact_length=16)
    wrapped_master_key = WrappedKeyField()

    def validate_iterations(self, value):
        if value < settings.KDF_MIN_ITERATIONS:
            raise serializers.ValidationError(f"Must be at least {settings.KDF_MIN_ITERATIONS}.")
        return value


class PreloginSerializer(serializers.Serializer):
    username = UsernameField()


class RegisterSerializer(KdfParamsMixin):
    username = UsernameField()
    auth_key = Key256Field()
    recovery_wrapped_master_key = WrappedKeyField()
    recovery_auth = Key256Field()


class LoginSerializer(serializers.Serializer):
    username = UsernameField()
    auth_key = Key256Field()


class RecoverSerializer(KdfParamsMixin):
    username = UsernameField()
    recovery_auth = Key256Field()
    auth_key = Key256Field(help_text="The new auth key, derived from the new passphrase.")


class RecoveryBundleSerializer(serializers.Serializer):
    username = UsernameField()
    recovery_auth = Key256Field()
