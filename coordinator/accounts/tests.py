import base64
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import AuthToken, KeyBundle
from .throttling import AuthIPRateThrottle, AuthUsernameRateThrottle

User = get_user_model()


def b64(n):
    return base64.b64encode(os.urandom(n)).decode()


def registration(username="thandi", **overrides):
    payload = {
        "username": username,
        "auth_key": b64(32),
        "kdf": "pbkdf2-sha256",
        "iterations": 600_000,
        "salt": b64(16),
        "wrapped_master_key": b64(61),
        "recovery_wrapped_master_key": b64(61),
        "recovery_auth": b64(32),
    }
    payload.update(overrides)
    return payload


class AuthTestCase(APITestCase):
    def setUp(self):
        cache.clear()

    def register(self, **kwargs):
        payload = registration(**kwargs)
        response = self.client.post("/api/auth/register", payload, format="json")
        return payload, response

    def use_token(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")


class RegisterTests(AuthTestCase):
    def test_register_returns_session_and_bundle(self):
        payload, response = self.register()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["username"], "thandi")
        self.assertEqual(response.data["key_bundle"]["wrapped_master_key"], payload["wrapped_master_key"])
        self.assertEqual(response.data["key_bundle"]["salt"], payload["salt"])
        self.assertTrue(response.data["token"])

    def test_auth_key_and_recovery_auth_are_hashed_at_rest(self):
        payload, _ = self.register()
        user = User.objects.get(username="thandi")
        self.assertNotIn(payload["auth_key"], user.password)
        self.assertTrue(user.check_password(payload["auth_key"]))
        self.assertNotIn(payload["recovery_auth"], user.key_bundle.recovery_verifier)

    def test_usernames_are_case_insensitive(self):
        self.register(username="Thandi")
        _, response = self.register(username="  THANDI ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.data)

    def test_rejects_weak_kdf(self):
        _, response = self.register(iterations=10_000)
        self.assertEqual(response.status_code, 400)
        self.assertIn("iterations", response.data)

    def test_rejects_wrong_length_keys(self):
        _, response = self.register(wrapped_master_key=b64(60), auth_key=b64(16))
        self.assertEqual(response.status_code, 400)
        self.assertIn("wrapped_master_key", response.data)
        self.assertIn("auth_key", response.data)

    def test_rejects_invalid_base64(self):
        _, response = self.register(salt="not base64!!")
        self.assertEqual(response.status_code, 400)

    def test_rejects_unknown_kdf(self):
        _, response = self.register(kdf="md5")
        self.assertEqual(response.status_code, 400)


class PreloginTests(AuthTestCase):
    def test_returns_stored_params(self):
        payload, _ = self.register()
        response = self.client.post("/api/auth/prelogin", {"username": "THANDI"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"kdf": "pbkdf2-sha256", "iterations": 600_000, "salt": payload["salt"]})

    def test_unknown_usernames_get_stable_believable_params(self):
        first = self.client.post("/api/auth/prelogin", {"username": "ghost"}, format="json").data
        second = self.client.post("/api/auth/prelogin", {"username": "ghost"}, format="json").data
        other = self.client.post("/api/auth/prelogin", {"username": "phantom"}, format="json").data
        self.assertEqual(first, second)
        self.assertNotEqual(first["salt"], other["salt"])
        self.assertEqual(len(base64.b64decode(first["salt"])), 16)
        self.assertEqual(set(first), {"kdf", "iterations", "salt"})

    def test_is_rate_limited_per_ip(self):
        rates = {"auth": "3/minute", "auth_user": "100/minute"}
        with mock.patch.object(AuthIPRateThrottle, "THROTTLE_RATES", rates), mock.patch.object(
            AuthUsernameRateThrottle, "THROTTLE_RATES", rates
        ):
            codes = [
                self.client.post("/api/auth/prelogin", {"username": f"ghost{i}"}, format="json").status_code
                for i in range(4)
            ]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_is_rate_limited_per_username_across_addresses(self):
        rates = {"auth": "100/minute", "auth_user": "2/minute"}
        with mock.patch.object(AuthIPRateThrottle, "THROTTLE_RATES", rates), mock.patch.object(
            AuthUsernameRateThrottle, "THROTTLE_RATES", rates
        ):
            codes = [
                self.client.post(
                    "/api/auth/login",
                    {"username": "Target" if i % 2 else "target", "auth_key": b64(32)},
                    format="json",
                    REMOTE_ADDR=f"10.0.0.{i}",
                ).status_code
                for i in range(3)
            ]
            other = self.client.post("/api/auth/login", {"username": "someone", "auth_key": b64(32)}, format="json")
        self.assertEqual(codes, [401, 401, 429])
        self.assertEqual(other.status_code, 401)


class LoginTests(AuthTestCase):
    def test_login_and_use_token(self):
        payload, _ = self.register()
        self.client.credentials()
        response = self.client.post(
            "/api/auth/login", {"username": "thandi", "auth_key": payload["auth_key"]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.use_token(response.data["token"])
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["username"], "thandi")
        self.assertEqual(me.data["key_bundle"]["wrapped_master_key"], payload["wrapped_master_key"])

    def test_wrong_and_unknown_get_the_same_answer(self):
        self.register()
        wrong = self.client.post("/api/auth/login", {"username": "thandi", "auth_key": b64(32)}, format="json")
        unknown = self.client.post("/api/auth/login", {"username": "nobody", "auth_key": b64(32)}, format="json")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.data, unknown.data)

    def test_tokens_are_stored_hashed(self):
        _, response = self.register()
        raw = response.data["token"]
        self.assertFalse(AuthToken.objects.filter(key_hash=raw).exists())
        self.assertEqual(AuthToken.objects.count(), 1)

    def test_logout_signs_out_only_this_device(self):
        payload, first = self.register()
        second = self.client.post(
            "/api/auth/login", {"username": "thandi", "auth_key": payload["auth_key"]}, format="json"
        )
        self.use_token(first.data["token"])
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.use_token(second.data["token"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_expired_tokens_are_rejected(self):
        _, response = self.register()
        AuthToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.use_token(response.data["token"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_use_slides_expiry_forward(self):
        _, response = self.register()
        old = timezone.now() - timedelta(days=1)
        AuthToken.objects.update(last_used_at=old, expires_at=old + AuthToken.TTL)
        self.use_token(response.data["token"])
        self.client.get("/api/auth/me")
        token = AuthToken.objects.get()
        self.assertGreater(token.expires_at, timezone.now() + AuthToken.TTL - timedelta(minutes=1))

    def test_requires_token(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.use_token("garbage")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)


class RecoveryTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.payload, response = self.register()
        self.old_token = response.data["token"]

    def recover_payload(self, **overrides):
        data = {
            "username": "thandi",
            "recovery_auth": self.payload["recovery_auth"],
            "auth_key": b64(32),
            "kdf": "pbkdf2-sha256",
            "iterations": 600_000,
            "salt": b64(16),
            "wrapped_master_key": b64(61),
        }
        data.update(overrides)
        return data

    def test_bundle_needs_the_recovery_key(self):
        good = self.client.post(
            "/api/auth/recovery-bundle",
            {"username": "thandi", "recovery_auth": self.payload["recovery_auth"]},
            format="json",
        )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.data, {"recovery_wrapped_master_key": self.payload["recovery_wrapped_master_key"]})

        bad = self.client.post("/api/auth/recovery-bundle", {"username": "thandi", "recovery_auth": b64(32)}, format="json")
        ghost = self.client.post("/api/auth/recovery-bundle", {"username": "ghost", "recovery_auth": b64(32)}, format="json")
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(ghost.status_code, 401)
        self.assertEqual(bad.data, ghost.data)

    def test_recover_swaps_passphrase_and_signs_out_everywhere(self):
        new = self.recover_payload()
        response = self.client.post("/api/auth/recover", new, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["key_bundle"]["wrapped_master_key"], new["wrapped_master_key"])
        # Recovery-wrapped key is untouched, so the same recovery key keeps working.
        self.assertEqual(
            response.data["key_bundle"]["recovery_wrapped_master_key"], self.payload["recovery_wrapped_master_key"]
        )

        self.use_token(self.old_token)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.use_token(response.data["token"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

        self.client.credentials()
        old_login = self.client.post(
            "/api/auth/login", {"username": "thandi", "auth_key": self.payload["auth_key"]}, format="json"
        )
        new_login = self.client.post("/api/auth/login", {"username": "thandi", "auth_key": new["auth_key"]}, format="json")
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    def test_recover_with_wrong_key_changes_nothing(self):
        before = KeyBundle.objects.get().wrapped_master_key
        response = self.client.post("/api/auth/recover", self.recover_payload(recovery_auth=b64(32)), format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(KeyBundle.objects.get().wrapped_master_key, before)
        self.use_token(self.old_token)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
