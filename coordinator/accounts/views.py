"""
Sign-up, sign-in and recovery without the server ever learning the passphrase.

The browser stretches the passphrase with PBKDF2 and splits the result with HKDF
into two unrelated keys: an auth key (sent here and hashed again like a normal
password) and a key-encryption key (never leaves the browser). Knowing the auth
key tells the server nothing about the key-encryption key.
"""

import base64
import hashlib
import hmac

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AuthToken, KeyBundle
from .throttling import AuthIPRateThrottle, AuthUsernameRateThrottle
from .serializers import (
    LoginSerializer,
    PreloginSerializer,
    RecoverSerializer,
    RecoveryBundleSerializer,
    RegisterSerializer,
)

User = get_user_model()

INVALID_CREDENTIALS = {"detail": "Invalid username or passphrase."}
INVALID_RECOVERY = {"detail": "Invalid username or recovery key."}

# Used to spend the same hashing time when the account doesn't exist.
_DUMMY_HASH = make_password("timing-equaliser")


def _session_payload(user, raw_token):
    return {
        "token": raw_token,
        "username": user.username,
        "key_bundle": user.key_bundle.as_payload(),
    }


class AuthEndpoint(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AuthIPRateThrottle, AuthUsernameRateThrottle]
    throttle_scope = "auth"


class PreloginView(AuthEndpoint):
    """
    Returns the KDF parameters for a username. Unknown usernames get stable,
    believable parameters so this endpoint can't be used to list accounts.
    """

    def post(self, request):
        serializer = PreloginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]

        bundle = KeyBundle.objects.filter(user__username=username).first()
        if bundle:
            return Response(bundle.public_params())

        fake_salt = hmac.new(
            settings.SECRET_KEY.encode(), f"prelogin:{username}".encode(), hashlib.sha256
        ).digest()[:16]
        return Response(
            {
                "kdf": KeyBundle.KDF_PBKDF2_SHA256,
                "iterations": settings.KDF_ITERATIONS,
                "salt": base64.b64encode(fake_salt).decode("ascii"),
            }
        )


class RegisterView(AuthEndpoint):
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            with transaction.atomic():
                user = User(username=data["username"])
                user.set_password(data["auth_key"])
                user.save()
                KeyBundle.objects.create(
                    user=user,
                    kdf=data["kdf"],
                    kdf_iterations=data["iterations"],
                    kdf_salt=data["salt"],
                    wrapped_master_key=data["wrapped_master_key"],
                    recovery_wrapped_master_key=data["recovery_wrapped_master_key"],
                    recovery_verifier=make_password(data["recovery_auth"]),
                )
        except IntegrityError:
            return Response({"username": ["That username is taken."]}, status=status.HTTP_400_BAD_REQUEST)

        _, raw = AuthToken.issue(user)
        return Response(_session_payload(user, raw), status=status.HTTP_201_CREATED)


class LoginView(AuthEndpoint):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = authenticate(request, username=data["username"], password=data["auth_key"])
        if user is None or not hasattr(user, "key_bundle"):
            return Response(INVALID_CREDENTIALS, status=status.HTTP_401_UNAUTHORIZED)

        _, raw = AuthToken.issue(user)
        return Response(_session_payload(user, raw))


def _verified_recovery_bundle(username, recovery_auth):
    """The key bundle if `recovery_auth` matches, otherwise None. Constant-ish time."""
    bundle = KeyBundle.objects.select_related("user").filter(user__username=username).first()
    if bundle is None:
        check_password(recovery_auth, _DUMMY_HASH)
        return None
    if not check_password(recovery_auth, bundle.recovery_verifier) or not bundle.user.is_active:
        return None
    return bundle


class RecoveryBundleView(AuthEndpoint):
    """
    Recovery step 1: proves the caller holds the recovery key and returns the
    master key wrapped under it, so the browser can re-wrap it for a new passphrase.
    """

    def post(self, request):
        serializer = RecoveryBundleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        bundle = _verified_recovery_bundle(data["username"], data["recovery_auth"])
        if bundle is None:
            return Response(INVALID_RECOVERY, status=status.HTTP_401_UNAUTHORIZED)
        return Response({"recovery_wrapped_master_key": bundle.recovery_wrapped_master_key})


class RecoverView(AuthEndpoint):
    """
    Recovery step 2: stores the master key re-wrapped under the new passphrase.
    Every existing session is signed out.
    """

    def post(self, request):
        serializer = RecoverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        bundle = _verified_recovery_bundle(data["username"], data["recovery_auth"])
        if bundle is None:
            return Response(INVALID_RECOVERY, status=status.HTTP_401_UNAUTHORIZED)

        user = bundle.user
        with transaction.atomic():
            user.set_password(data["auth_key"])
            user.save(update_fields=["password"])
            bundle.kdf = data["kdf"]
            bundle.kdf_iterations = data["iterations"]
            bundle.kdf_salt = data["salt"]
            bundle.wrapped_master_key = data["wrapped_master_key"]
            bundle.save()
            AuthToken.objects.filter(user=user).delete()

        _, raw = AuthToken.issue(user)
        return Response(_session_payload(user, raw))


class LogoutView(APIView):
    def post(self, request):
        if isinstance(request.auth, AuthToken):
            request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response({"username": request.user.username, "key_bundle": request.user.key_bundle.as_payload()})
